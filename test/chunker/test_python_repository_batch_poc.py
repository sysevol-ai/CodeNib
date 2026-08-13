# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Adversarial tests for the private Python repository-batch adapter."""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from codenib.code_chunker import CodeChunker
from codenib.code_chunking import python_repository_batch_poc as batch
from codenib.code_chunking.base import BaseCodeChunker, CodeChunk

EXPECTED_CONTRACT = {
    "abi_version": 1,
    "file_row_size": 8,
    "file_row_format": "<II",
    "span_row_size": 20,
    "span_row_format": "<IIB3xII",
    "grammar": "tree-sitter-python@v0.25.0",
    "runtime": "tree-sitter@v0.25.2",
    "semantic_profile": "python-definition-spans-v1",
    "ordering": "input-ordinal-then-span-order-v1",
    "supported_worker_counts": [1, 2, 4],
    "overflow_policy": "fail-closed-v1",
    "max_file_count": 10_000,
    "max_source_bytes": 1_000_000_000,
    "max_row_count": 10_000_000,
    "max_arena_bytes": 1_000_000_000,
}

BASE_CONFIG = {
    "id": "unit-test",
    "language": "python",
    "chunk_depth": 2,
    "l2_level_exclusive": True,
    "skeleton_mode": False,
    "include_header_epilogue": True,
    "max_lines_per_chunk": 2,
    "filter_tests": True,
    "strict": True,
    "repository_filter_policy": 3,
    "consumer_contract": "unit-test",
}


def _payload(
    sources,
    definitions_by_file=None,
    *,
    worker_count=1,
    **overrides,
):
    if definitions_by_file is None:
        definitions_by_file = [[] for _ in sources]
    arena = bytearray()
    rows = bytearray()
    file_rows = bytearray()
    row_offset = 0
    for definitions in definitions_by_file:
        file_rows.extend(batch._FILE_ROW.pack(row_offset, len(definitions)))
        for start_line, end_line, kind_id, name in definitions:
            encoded_name = name.encode("utf-8")
            name_offset = len(arena)
            arena.extend(encoded_name)
            rows.extend(
                batch._SPAN_ROW.pack(
                    start_line,
                    end_line,
                    kind_id,
                    name_offset,
                    len(encoded_name),
                )
            )
        row_offset += len(definitions)
    payload = {
        "abi_version": 1,
        "file_count": len(sources),
        "source_bytes": sum(len(source.encode("utf-8")) for source in sources),
        "row_count": row_offset,
        "worker_count": worker_count,
        "file_rows": bytes(file_rows),
        "rows": bytes(rows),
        "arena": bytes(arena),
        "batch_wall_ns": 9_000_000,
        "parse_work_ns": 12_000_000,
        "extract_encode_work_ns": 3_000_000,
        "ordered_merge_ns": 1_000_000,
    }
    payload.update(overrides)
    return payload


class FakeCore:
    def __init__(self, payload_factory=None, *, contract=None, failure=None):
        self.payload_factory = payload_factory
        self.contract = dict(EXPECTED_CONTRACT if contract is None else contract)
        self.failure = failure
        self.calls = []
        self.contract_calls = 0

    def python_chunk_batch_poc_contract(self):
        self.contract_calls += 1
        return dict(self.contract)

    def extract_python_repository_chunk_spans(self, sources, **kwargs):
        self.calls.append((list(sources), dict(kwargs)))
        if self.failure is not None:
            raise self.failure
        if self.payload_factory is None:
            return _payload(sources, worker_count=kwargs["worker_count"])
        return self.payload_factory(list(sources), **kwargs)


@pytest.fixture
def repository(tmp_path):
    (tmp_path / "empty.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "a.py").write_text(
        "class Service:\n    def run(self):\n        return 2\n", encoding="utf-8"
    )
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text(
        "# header\n\ndef alpha():\n    return 1\n\nTAIL = alpha()\n",
        encoding="utf-8",
    )
    (tmp_path / "test_filtered.py").write_text(
        "def excluded():\n    return 0\n", encoding="utf-8"
    )
    return tmp_path


def _semantic_payload(sources, *, worker_count, **_kwargs):
    definitions = []
    for source in sources:
        if source.startswith("VALUE"):
            definitions.append([])
        elif source.startswith("class Service"):
            definitions.append([(1, 2, 3, "Service.run")])
        elif source.startswith("# header"):
            definitions.append([(2, 3, 1, "alpha")])
        else:
            raise AssertionError(f"unexpected selected source: {source!r}")
    return _payload(sources, definitions, worker_count=worker_count)


def _reference(repository, config=None):
    return batch.run_python_repository_batch(
        repository,
        config=BASE_CONFIG if config is None else config,
        mode="off",
        worker_count=1,
    )


def test_default_mode_is_off_and_never_loads_native(repository, monkeypatch):
    monkeypatch.delenv("CODENIB_NATIVE_PYTHON_CHUNK_BATCH", raising=False)
    monkeypatch.setattr(
        batch,
        "_load_native_core",
        lambda: pytest.fail("default-off adapter loaded the native core"),
    )

    result = batch.run_python_repository_batch(
        repository, config=BASE_CONFIG, worker_count=1
    )

    assert type(result.chunks) is list
    assert all(type(chunk) is CodeChunk for chunk in result.chunks)
    assert result.nodes == sorted(set(result.nodes))
    assert result.diagnostics["backend"] == "python"
    assert result.diagnostics["fallback_count"] == 0
    assert result.diagnostics["batch_call_count"] == 0
    assert result.diagnostics["worker_count"] is None


def test_explicit_mode_overrides_environment(repository, monkeypatch):
    monkeypatch.setenv("CODENIB_NATIVE_PYTHON_CHUNK_BATCH", "required")
    monkeypatch.setattr(
        batch,
        "_load_native_core",
        lambda: pytest.fail("explicit off mode loaded the native core"),
    )

    assert (
        batch.run_python_repository_batch(
            repository, config=BASE_CONFIG, mode="off", worker_count=1
        ).diagnostics["backend"]
        == "python"
    )


def test_off_does_not_mislabel_materialized_chunks_as_definition_spans(tmp_path):
    (tmp_path / "sample.py").write_text(
        "# header\n\ndef work():\n    first = 1\n    second = 2\n    return first + second\n",
        encoding="utf-8",
    )
    config = {
        **BASE_CONFIG,
        "include_header_epilogue": True,
        "max_lines_per_chunk": 2,
    }

    result = batch.run_python_repository_batch(
        tmp_path, config=config, mode="off", worker_count=1
    )

    assert len(result.chunks) > 1
    assert result.diagnostics["counters"]["chunk_count"] == len(result.chunks)
    assert result.diagnostics["counters"]["span_count"] is None


@pytest.mark.parametrize("mode", ["", "sometimes", 1, True, None])
def test_invalid_explicit_or_environment_mode_fails(repository, monkeypatch, mode):
    if mode is None:
        monkeypatch.setenv("CODENIB_NATIVE_PYTHON_CHUNK_BATCH", "sometimes")
    with pytest.raises((TypeError, ValueError), match="must be|mode"):
        batch.run_python_repository_batch(
            repository, config=BASE_CONFIG, mode=mode, worker_count=1
        )


@pytest.mark.parametrize("worker_count", [True, False, 0, -1, 3, 1.0, "1"])
def test_adapter_rejects_non_exact_worker_counts(repository, worker_count):
    with pytest.raises(ValueError, match="worker_count"):
        batch.run_python_repository_batch(
            repository,
            config=BASE_CONFIG,
            mode="required",
            worker_count=worker_count,
        )


@pytest.mark.parametrize("worker_count", [1, 2, 4])
def test_candidate_matches_complete_legacy_result_for_every_worker(
    repository, monkeypatch, worker_count
):
    reference = _reference(repository)
    core = FakeCore(_semantic_payload)
    monkeypatch.setattr(batch, "_load_native_core", lambda: core)

    candidate = batch.run_python_repository_batch(
        repository,
        config=BASE_CONFIG,
        mode="required",
        worker_count=worker_count,
    )

    assert candidate.chunks == reference.chunks
    assert candidate.nodes == reference.nodes
    assert candidate.nodes == sorted(set(candidate.nodes))
    assert all(type(chunk) is CodeChunk for chunk in candidate.chunks)
    assert candidate.diagnostics == {
        "backend": "native-repository-batch-poc",
        "fallback_count": 0,
        "batch_call_count": 1,
        "worker_count": worker_count,
        "contract": EXPECTED_CONTRACT,
        "counters": {
            "file_count": 3,
            "source_bytes": sum(
                path.stat().st_size
                for path in (
                    repository / "empty.py",
                    repository / "other" / "a.py",
                    repository / "pkg" / "a.py",
                )
            ),
            "span_count": 2,
            "chunk_count": len(reference.chunks),
            "node_count": len(reference.nodes),
        },
        "stages": candidate.diagnostics["stages"],
    }
    assert set(candidate.diagnostics["stages"]) == set(batch._STAGE_FIELDS)
    assert all(value >= 0 for value in candidate.diagnostics["stages"].values())
    selected_sources, kwargs = core.calls[0]
    assert selected_sources == [
        (repository / "empty.py").read_text(encoding="utf-8"),
        (repository / "other" / "a.py").read_text(encoding="utf-8"),
        (repository / "pkg" / "a.py").read_text(encoding="utf-8"),
    ]
    assert kwargs == {
        "chunk_depth": 2,
        "l2_level_exclusive": True,
        "worker_count": worker_count,
    }
    assert core.contract_calls == 1


def test_candidate_reports_raw_disk_bytes_separately_from_decoded_input(
    tmp_path, monkeypatch
):
    raw = b"# \xff\nVALUE = 1\n"
    (tmp_path / "invalid.py").write_bytes(raw)
    observed = {}

    def payload_factory(sources, *, worker_count, **_kwargs):
        observed["encoded"] = sum(len(source.encode("utf-8")) for source in sources)
        return _payload(sources, worker_count=worker_count)

    monkeypatch.setattr(batch, "_load_native_core", lambda: FakeCore(payload_factory))
    result = batch.run_python_repository_batch(
        tmp_path,
        config=BASE_CONFIG,
        mode="required",
        worker_count=1,
    )

    assert observed["encoded"] > len(raw)
    assert result.diagnostics["counters"]["source_bytes"] == len(raw)


def test_zero_file_repository_still_makes_one_batch_call(tmp_path, monkeypatch):
    core = FakeCore()
    monkeypatch.setattr(batch, "_load_native_core", lambda: core)

    result = batch.run_python_repository_batch(
        tmp_path, config=BASE_CONFIG, mode="required", worker_count=4
    )

    assert result.chunks == []
    assert result.nodes == []
    assert result.diagnostics["batch_call_count"] == 1
    assert result.diagnostics["worker_count"] == 4
    assert core.calls == [
        (
            [],
            {
                "chunk_depth": 2,
                "l2_level_exclusive": True,
                "worker_count": 4,
            },
        )
    ]


def test_parallel_adapter_calls_do_not_share_results(repository, monkeypatch):
    monkeypatch.setattr(batch, "_load_native_core", lambda: FakeCore(_semantic_payload))

    def run(_index):
        return batch.run_python_repository_batch(
            repository, config=BASE_CONFIG, mode="required", worker_count=2
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(run, range(8)))

    assert all(result.chunks == results[0].chunks for result in results)
    assert len({id(result.chunks) for result in results}) == len(results)
    assert len({id(result.nodes) for result in results}) == len(results)


def test_required_fails_closed_without_batch_api(repository, monkeypatch):
    monkeypatch.setattr(batch, "_load_native_core", lambda: object())

    with pytest.raises(RuntimeError, match="does not include"):
        batch.run_python_repository_batch(
            repository, config=BASE_CONFIG, mode="required", worker_count=1
        )


def test_required_strict_source_read_failure_stops_before_batch(
    repository, monkeypatch
):
    original = BaseCodeChunker._read_source

    def fail_one_source(file_path):
        if Path(file_path) == repository / "other" / "a.py":
            raise OSError("synthetic source read failure")
        return original(file_path)

    core = FakeCore(_semantic_payload)
    monkeypatch.setattr(BaseCodeChunker, "_read_source", staticmethod(fail_one_source))
    monkeypatch.setattr(batch, "_load_native_core", lambda: core)

    with pytest.raises(
        RuntimeError, match=r"other/a\.py: synthetic source read failure"
    ):
        batch.run_python_repository_batch(
            repository, config=BASE_CONFIG, mode="required", worker_count=1
        )

    assert core.calls == []


def test_non_strict_source_read_failure_skips_before_single_batch(
    repository, monkeypatch
):
    original = BaseCodeChunker._read_source

    def fail_one_source(file_path):
        if Path(file_path) == repository / "other" / "a.py":
            raise OSError("synthetic source read failure")
        return original(file_path)

    config = {**BASE_CONFIG, "strict": False}
    core = FakeCore(_semantic_payload)
    monkeypatch.setattr(BaseCodeChunker, "_read_source", staticmethod(fail_one_source))
    monkeypatch.setattr(batch, "_load_native_core", lambda: core)

    established = batch.run_python_repository_batch(
        repository, config=config, mode="off", worker_count=1
    )
    candidate = batch.run_python_repository_batch(
        repository, config=config, mode="required", worker_count=1
    )

    assert candidate.chunks == established.chunks
    assert candidate.nodes == established.nodes
    assert len(core.calls) == 1
    selected_sources, _arguments = core.calls[0]
    assert selected_sources == [
        (repository / "empty.py").read_text(encoding="utf-8"),
        (repository / "pkg" / "a.py").read_text(encoding="utf-8"),
    ]
    assert candidate.diagnostics["batch_call_count"] == 1
    assert candidate.diagnostics["counters"] == {
        "file_count": 2,
        "source_bytes": (repository / "empty.py").stat().st_size
        + (repository / "pkg" / "a.py").stat().st_size,
        "span_count": 1,
        "chunk_count": len(candidate.chunks),
        "node_count": len(candidate.nodes),
    }


def test_auto_fallback_is_one_whole_repository_rerun(repository, monkeypatch):
    core = FakeCore(failure=RuntimeError("synthetic native failure"))
    monkeypatch.setattr(batch, "_load_native_core", lambda: core)
    original = CodeChunker.chunk_repository
    legacy_calls = []

    def tracked_legacy(self, repo_path, *, strict=False):
        legacy_calls.append((repo_path, strict))
        return original(self, repo_path, strict=strict)

    monkeypatch.setattr(CodeChunker, "chunk_repository", tracked_legacy)
    result = batch.run_python_repository_batch(
        repository, config=BASE_CONFIG, mode="auto", worker_count=2
    )

    assert result.chunks == _reference(repository).chunks
    assert result.diagnostics["backend"] == "python-fallback"
    assert result.diagnostics["fallback_count"] == 1
    assert result.diagnostics["batch_call_count"] == 1
    assert result.diagnostics["worker_count"] is None
    assert result.diagnostics["counters"]["span_count"] is None
    # One fallback call plus the explicit reference call made after the result.
    assert len(legacy_calls) == 2


def test_auto_contract_failure_falls_back_before_batch_call(repository, monkeypatch):
    contract = {**EXPECTED_CONTRACT, "abi_version": 2}
    monkeypatch.setattr(batch, "_load_native_core", lambda: FakeCore(contract=contract))

    result = batch.run_python_repository_batch(
        repository, config=BASE_CONFIG, mode="auto", worker_count=1
    )

    assert result.diagnostics["backend"] == "python-fallback"
    assert result.diagnostics["fallback_count"] == 1
    assert result.diagnostics["batch_call_count"] == 0
    assert result.diagnostics["contract"] is None


def test_auto_discovery_failure_records_elapsed_stage_before_fallback(
    repository, monkeypatch
):
    monkeypatch.setattr(
        batch,
        "_read_selected_sources",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("synthetic discovery failure")
        ),
    )

    result = batch.run_python_repository_batch(
        repository, config=BASE_CONFIG, mode="auto", worker_count=1
    )

    assert result.diagnostics["backend"] == "python-fallback"
    assert result.diagnostics["fallback_count"] == 1
    assert result.diagnostics["batch_call_count"] == 0
    assert result.diagnostics["stages"]["discovery_read_wall_seconds"] > 0.0


def test_auto_discards_all_candidate_output_after_decode_failure(
    repository, monkeypatch
):
    def malformed(sources, *, worker_count, **_kwargs):
        return _payload(
            sources,
            [[(0, 100, 1, "bad")], [], []],
            worker_count=worker_count,
        )

    monkeypatch.setattr(batch, "_load_native_core", lambda: FakeCore(malformed))

    result = batch.run_python_repository_batch(
        repository, config=BASE_CONFIG, mode="auto", worker_count=1
    )

    assert result.chunks == _reference(repository).chunks
    assert result.nodes == _reference(repository).nodes
    assert result.diagnostics["fallback_count"] == 1
    assert result.diagnostics["batch_call_count"] == 1


def test_auto_discards_partially_materialized_candidate(repository, monkeypatch):
    from codenib.code_chunking.python_chunker import PythonCodeChunker

    reference = _reference(repository)
    monkeypatch.setattr(batch, "_load_native_core", lambda: FakeCore(_semantic_payload))
    original = PythonCodeChunker._generate_chunks
    call_count = 0

    def fail_during_second_file(self, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("synthetic materialization failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PythonCodeChunker, "_generate_chunks", fail_during_second_file)

    result = batch.run_python_repository_batch(
        repository, config=BASE_CONFIG, mode="auto", worker_count=1
    )

    assert result.chunks == reference.chunks
    assert result.nodes == reference.nodes
    assert result.diagnostics["backend"] == "python-fallback"
    assert result.diagnostics["fallback_count"] == 1
    assert result.diagnostics["batch_call_count"] == 1
    assert call_count == 5


@pytest.mark.parametrize(
    "failure",
    [MemoryError(), MemoryError("synthetic exhaustion")],
)
def test_memory_error_never_falls_back(repository, monkeypatch, failure):
    core = FakeCore(failure=failure)
    monkeypatch.setattr(batch, "_load_native_core", lambda: core)
    monkeypatch.setattr(
        CodeChunker,
        "chunk_repository",
        lambda *_args, **_kwargs: pytest.fail("MemoryError triggered legacy work"),
    )

    with pytest.raises(MemoryError):
        batch.run_python_repository_batch(
            repository, config=BASE_CONFIG, mode="auto", worker_count=1
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("abi_version", True),
        ("abi_version", 2),
        ("file_row_size", 9),
        ("file_row_format", "@II"),
        ("span_row_size", 24),
        ("span_row_format", "@IIB3xII"),
        ("grammar", "tree-sitter-python@v0.24.0"),
        ("runtime", "tree-sitter@v0.24.0"),
        ("semantic_profile", "python-definition-spans-v2"),
        ("ordering", "completion-order"),
        ("supported_worker_counts", [1, 4]),
        ("overflow_policy", "wrap"),
        ("max_file_count", True),
        ("max_source_bytes", 1),
        ("max_row_count", -1),
        ("max_arena_bytes", 0),
    ],
)
def test_contract_validation_rejects_every_mismatch(field, value):
    core = FakeCore(contract={**EXPECTED_CONTRACT, field: value})

    with pytest.raises(RuntimeError, match=field):
        batch._validate_native_contract(core)


def test_contract_validation_rejects_extra_or_missing_fields():
    extra = {**EXPECTED_CONTRACT, "future": 1}
    missing = dict(EXPECTED_CONTRACT)
    missing.pop("ordering")

    with pytest.raises(RuntimeError, match="schema"):
        batch._validate_native_contract(FakeCore(contract=extra))
    with pytest.raises(RuntimeError, match="schema"):
        batch._validate_native_contract(FakeCore(contract=missing))


def _decode(payload, sources=("value\n",), *, worker_count=1, contract=None):
    return batch._decode_native_batch_payload(
        payload,
        sources=list(sources),
        requested_worker_count=worker_count,
        contract=EXPECTED_CONTRACT if contract is None else contract,
    )


def test_decoder_reads_file_and_span_tables_in_input_order():
    sources = ["def one():\n    pass\n", "VALUE = 1\n", "class C:\n    pass\n"]
    payload = _payload(
        sources,
        [[(0, 1, 1, "one")], [], [(0, 1, 2, "C")]],
        worker_count=2,
    )

    decoded, timings, row_count = _decode(payload, sources=sources, worker_count=2)

    assert [
        [
            (node.start_point[0], node.end_point[0], name, kind)
            for node, name, kind in rows
        ]
        for rows in decoded
    ] == [[(0, 1, "one", "function")], [], [(0, 1, "C", "class")]]
    assert timings == {
        "native_batch_wall_seconds": 0.009,
        "native_parse_work_seconds": 0.012,
        "native_extract_encode_work_seconds": 0.003,
        "native_ordered_merge_seconds": 0.001,
    }
    assert row_count == 2


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("abi_version", True, "ABI"),
        ("abi_version", 2, "ABI"),
        ("file_count", True, "file_count"),
        ("file_count", -1, "file_count"),
        ("file_count", 2, "file count mismatch"),
        ("source_bytes", True, "source_bytes"),
        ("source_bytes", -1, "source_bytes"),
        ("source_bytes", 99, "source byte count mismatch"),
        ("row_count", True, "row_count"),
        ("row_count", -1, "row_count"),
        ("worker_count", True, "worker_count"),
        ("worker_count", 2, "worker count mismatch"),
        ("batch_wall_ns", True, "batch_wall_ns"),
        ("batch_wall_ns", -1, "batch_wall_ns"),
        ("parse_work_ns", True, "parse_work_ns"),
        ("parse_work_ns", -1, "parse_work_ns"),
        ("extract_encode_work_ns", True, "extract_encode_work_ns"),
        ("extract_encode_work_ns", -1, "extract_encode_work_ns"),
        ("ordered_merge_ns", True, "ordered_merge_ns"),
        ("ordered_merge_ns", -1, "ordered_merge_ns"),
    ],
)
def test_payload_scalar_validation(field, value, message):
    payload = _payload(["value\n"])
    payload[field] = value

    with pytest.raises(ValueError, match=message):
        _decode(payload)


def test_payload_requires_exact_schema():
    extra = _payload(["value\n"])
    extra["future"] = 1
    missing = _payload(["value\n"])
    missing.pop("arena")

    with pytest.raises(ValueError, match="schema"):
        _decode(extra)
    with pytest.raises(ValueError, match="schema"):
        _decode(missing)


@pytest.mark.parametrize("field", ["file_rows", "rows", "arena"])
@pytest.mark.parametrize("value", [bytearray(), memoryview(b""), ""])
def test_payload_buffers_must_be_exact_bytes(field, value):
    payload = _payload(["value\n"])
    payload[field] = value

    with pytest.raises(TypeError, match="must be bytes"):
        _decode(payload)


def test_file_table_rejects_size_gap_overlap_bounds_and_incomplete_coverage():
    sources = ["a\n", "b\n"]
    base = _payload(
        sources,
        [[(0, 0, 1, "a")], [(0, 0, 1, "b")]],
    )
    malformed_tables = [
        b"",
        batch._FILE_ROW.pack(0, 1) + batch._FILE_ROW.pack(2, 0),
        batch._FILE_ROW.pack(0, 1) + batch._FILE_ROW.pack(0, 1),
        batch._FILE_ROW.pack(0, 1) + batch._FILE_ROW.pack(1, 2),
        batch._FILE_ROW.pack(0, 1) + batch._FILE_ROW.pack(1, 0),
    ]

    for table in malformed_tables:
        payload = dict(base)
        payload["file_rows"] = table
        with pytest.raises(ValueError):
            _decode(payload, sources=sources)


def test_span_table_rejects_wrong_byte_size():
    payload = _payload(["value\n"], [[(0, 0, 1, "value")]])
    payload["rows"] = payload["rows"][:-1]

    with pytest.raises(ValueError, match="span table"):
        _decode(payload)


@pytest.mark.parametrize(
    ("definitions", "message"),
    [
        ([(1, 0, 1, "x")], "reversed"),
        ([(0, 3, 1, "x")], "line bounds"),
        ([(1, 1, 1, "x"), (0, 0, 1, "y")], "not sorted"),
        ([(0, 0, 0, "x")], "kind"),
        ([(0, 0, 4, "x")], "kind"),
        ([(0, 0, 1, "")], "empty"),
    ],
)
def test_span_validation_rejects_invalid_values(definitions, message):
    payload = _payload(["a\nb\n"], [definitions])

    with pytest.raises(ValueError, match=message):
        _decode(payload, sources=["a\nb\n"])


def test_span_padding_must_be_zero():
    payload = _payload(["value\n"], [[(0, 0, 1, "value")]])
    rows = bytearray(payload["rows"])
    rows[9] = 1
    payload["rows"] = bytes(rows)

    with pytest.raises(ValueError, match="padding"):
        _decode(payload)


def test_name_arena_rejects_bounds_and_invalid_utf8():
    payload = _payload(["value\n"], [[(0, 0, 1, "value")]])
    rows = bytearray(payload["rows"])
    rows[12:16] = (99).to_bytes(4, "little")
    payload["rows"] = bytes(rows)
    with pytest.raises(ValueError, match="arena bounds"):
        _decode(payload)

    payload = _payload(["value\n"], [[(0, 0, 1, "value")]])
    payload["arena"] = b"\xffalue"
    with pytest.raises(ValueError, match="valid UTF-8"):
        _decode(payload)

    payload = _payload(["value\n"])
    payload["arena"] = b"unused\xff"
    with pytest.raises(ValueError, match="valid UTF-8"):
        _decode(payload)


def test_ordered_merge_time_cannot_exceed_batch_wall():
    payload = _payload(["value\n"], batch_wall_ns=1, ordered_merge_ns=2)

    with pytest.raises(ValueError, match="ordered merge exceeds"):
        _decode(payload)


def test_decoder_enforces_every_resource_cap():
    empty = _payload([])
    with pytest.raises(ValueError, match="file count exceeds"):
        _decode(
            empty,
            sources=[],
            contract={**EXPECTED_CONTRACT, "max_file_count": -1},
        )

    source = _payload(["value\n"])
    with pytest.raises(ValueError, match="source bytes exceed"):
        _decode(
            source,
            contract={**EXPECTED_CONTRACT, "max_source_bytes": 1},
        )

    row = _payload(["value\n"], [[(0, 0, 1, "value")]])
    with pytest.raises(ValueError, match="row count exceeds"):
        _decode(row, contract={**EXPECTED_CONTRACT, "max_row_count": 0})
    with pytest.raises(ValueError, match="arena exceeds"):
        _decode(row, contract={**EXPECTED_CONTRACT, "max_arena_bytes": 0})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("language", "javascript"),
        ("chunk_depth", True),
        ("chunk_depth", 0),
        ("l2_level_exclusive", 1),
        ("skeleton_mode", 0),
        ("include_header_epilogue", None),
        ("max_lines_per_chunk", True),
        ("max_lines_per_chunk", 0),
        ("filter_tests", 1),
        ("strict", "yes"),
        ("repository_filter_policy", True),
        ("repository_filter_policy", 2),
    ],
)
def test_invalid_config_fails_closed(repository, field, value):
    config = {**BASE_CONFIG, field: value}

    with pytest.raises((TypeError, ValueError, RuntimeError)):
        batch.run_python_repository_batch(
            repository, config=config, mode="required", worker_count=1
        )


def _native_batch_core():
    core = batch._load_native_core()
    if core is None:
        return None
    if not callable(getattr(core, "python_chunk_batch_poc_contract", None)):
        return None
    if not callable(getattr(core, "extract_python_repository_chunk_spans", None)):
        return None
    return core


REAL_PARITY_CONFIGS = [
    pytest.param(
        {
            **BASE_CONFIG,
            "id": "continuity_l2_exclusive_unsplit",
            "include_header_epilogue": False,
            "max_lines_per_chunk": None,
            "consumer_contract": "continuity",
        },
        id="continuity-unsplit",
    ),
    pytest.param(
        {
            **BASE_CONFIG,
            "id": "bm25_v8_l2_exclusive_headers_300",
            "include_header_epilogue": True,
            "max_lines_per_chunk": 300,
            "consumer_contract": "bm25-builder-schema-v8",
            "builder_schema": 8,
        },
        id="bm25-headers-300",
    ),
]


@pytest.fixture
def real_parity_repository(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "features.py").write_text(
        """\
# tête
from collections.abc import Callable

@trace(label="café")
async def transform[T](value: T) -> T:
    return value

@registered
class Service[T]:
    @classmethod
    async def créer(cls, value: T):
        return cls()

    def direct(self):
        return 3

RESULT: Callable | None = None
""",
        encoding="utf-8",
    )
    (tmp_path / "control_flow.py").write_text(
        """\
def outer():
    def nested():
        return 1
    return nested()

if ENABLED:
    def conditional():
        return 2

class Visible:
    if ENABLED:
        def conditional_method(self):
            return 3

    def direct(self):
        return 4
""",
        encoding="utf-8",
    )
    (tmp_path / "recovery.py").write_text(
        """\
def before_error():
    return 1

def broken(:
    missing

class Recovered:
    def after_error(self):
        return 2
""",
        encoding="utf-8",
    )
    long_body = "\n".join(f"    value += {index}" for index in range(620))
    (tmp_path / "long.py").write_text(
        f"# long header\n\ndef long_function():\n    value = 0\n{long_body}\n"
        "    return value\n\nLONG_RESULT = long_function()\n",
        encoding="utf-8",
    )
    (tmp_path / "declarations.py").write_text("VALUE = 1\n", encoding="utf-8")
    return tmp_path


@pytest.mark.skipif(
    _native_batch_core() is None, reason="native repository batch POC not built"
)
@pytest.mark.parametrize("config", REAL_PARITY_CONFIGS)
@pytest.mark.parametrize("worker_count", [1, 2, 4])
def test_real_binding_complete_repository_parity(
    real_parity_repository, config, worker_count
):
    established = batch.run_python_repository_batch(
        real_parity_repository,
        config=config,
        mode="off",
        worker_count=worker_count,
    )
    candidate = batch.run_python_repository_batch(
        real_parity_repository,
        config=config,
        mode="required",
        worker_count=worker_count,
    )

    assert candidate.chunks == established.chunks
    assert [tuple(chunk) for chunk in candidate.chunks] == [
        tuple(chunk) for chunk in established.chunks
    ]
    assert all(len(chunk) == 7 for chunk in candidate.chunks)
    assert candidate.nodes == established.nodes == sorted(set(established.nodes))
    assert candidate.diagnostics["backend"] == "native-repository-batch-poc"
    assert candidate.diagnostics["fallback_count"] == 0
    assert candidate.diagnostics["batch_call_count"] == 1
    assert candidate.diagnostics["worker_count"] == worker_count
    assert candidate.diagnostics["contract"] == EXPECTED_CONTRACT

    long_chunks = [chunk for chunk in candidate.chunks if chunk.name == "long_function"]
    if config["max_lines_per_chunk"] == 300:
        assert len(long_chunks) == 3
        assert all(
            chunk.end_line - chunk.start_line + 1 <= 300 for chunk in long_chunks
        )
        assert any(chunk.chunk_type == "header" for chunk in candidate.chunks)
        assert any(chunk.chunk_type == "epilogue" for chunk in candidate.chunks)
    else:
        assert len(long_chunks) == 1
        assert not any(
            chunk.chunk_type in {"header", "epilogue"} for chunk in candidate.chunks
        )


@pytest.mark.skipif(
    _native_batch_core() is None, reason="native repository batch POC not built"
)
def test_real_binding_exposes_exact_contract_and_payload():
    core = _native_batch_core()
    assert core.python_chunk_batch_poc_contract() == EXPECTED_CONTRACT

    payload = core.extract_python_repository_chunk_spans(
        [], chunk_depth=2, l2_level_exclusive=True, worker_count=4
    )

    assert set(payload) == batch._PAYLOAD_KEYS
    decoded, _timings, row_count = _decode(payload, sources=[], worker_count=4)
    assert decoded == []
    assert row_count == 0


@pytest.mark.skipif(
    _native_batch_core() is None, reason="native repository batch POC not built"
)
def test_real_binding_owns_items_returned_by_dynamic_sequence():
    core = _native_batch_core()

    class DynamicSources:
        def __init__(self, values):
            self.values = values

        def __len__(self):
            return len(self.values)

        def __getitem__(self, index):
            if index >= len(self.values):
                raise IndexError(index)
            # Force each access to create a temporary Python string.
            return self.values[index].encode("utf-8").decode("utf-8")

    values = [f"def generated_{index}():\n    return {index}\n" for index in range(32)]
    for _ in range(64):
        payload = core.extract_python_repository_chunk_spans(
            DynamicSources(values),
            chunk_depth=1,
            l2_level_exclusive=True,
            worker_count=4,
        )
        decoded, _timings, row_count = _decode(payload, sources=values, worker_count=4)
        assert row_count == len(values)
        assert [definitions[0][1] for definitions in decoded] == [
            f"generated_{index}" for index in range(len(values))
        ]


@pytest.mark.skipif(
    _native_batch_core() is None, reason="native repository batch POC not built"
)
def test_dynamic_sequence_is_stable_with_debug_python_allocator():
    core = _native_batch_core()
    core_dir = str(Path(core.__file__).resolve().parent)
    script = """
import codenib_core

class DynamicSources:
    def __init__(self, values):
        self.values = values
    def __len__(self):
        return len(self.values)
    def __getitem__(self, index):
        if index >= len(self.values):
            raise IndexError(index)
        return self.values[index].encode('utf-8').decode('utf-8')

values = [f'def generated_{index}():\\n    return {index}\\n' for index in range(64)]
for _ in range(128):
    payload = codenib_core.extract_python_repository_chunk_spans(
        DynamicSources(values),
        chunk_depth=1,
        l2_level_exclusive=True,
        worker_count=4,
    )
    assert payload['file_count'] == len(values)
    assert payload['row_count'] == len(values)
"""
    environment = dict(os.environ)
    environment["PYTHONMALLOC"] = "debug"
    environment["PYTHONPATH"] = os.pathsep.join(
        [core_dir, str(Path(__file__).resolve().parents[2])]
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(
    _native_batch_core() is None, reason="native repository batch POC not built"
)
@pytest.mark.parametrize(
    ("chunk_depth", "error_type"),
    [
        (True, TypeError),
        (False, TypeError),
        (1.0, TypeError),
        (-1, ValueError),
        (0, ValueError),
        (3, ValueError),
    ],
)
def test_real_binding_rejects_non_exact_chunk_depth(chunk_depth, error_type):
    core = _native_batch_core()
    with pytest.raises(error_type, match="chunk_depth"):
        core.extract_python_repository_chunk_spans(
            [],
            chunk_depth=chunk_depth,
            l2_level_exclusive=True,
            worker_count=1,
        )


@pytest.mark.skipif(
    _native_batch_core() is None, reason="native repository batch POC not built"
)
@pytest.mark.parametrize(
    ("worker_count", "error_type"),
    [
        (True, TypeError),
        (False, TypeError),
        (1.0, TypeError),
        (0, ValueError),
        (3, ValueError),
    ],
)
def test_real_binding_rejects_non_exact_worker_count(worker_count, error_type):
    core = _native_batch_core()
    with pytest.raises(error_type, match="worker_count"):
        core.extract_python_repository_chunk_spans(
            [],
            chunk_depth=2,
            l2_level_exclusive=True,
            worker_count=worker_count,
        )


@pytest.mark.skipif(
    _native_batch_core() is None, reason="native repository batch POC not built"
)
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("chunk_depth", 2**100),
        ("worker_count", -1),
        ("worker_count", 2**100),
    ],
)
def test_real_binding_integer_overflow_names_the_field(field, value):
    core = _native_batch_core()
    arguments = {
        "chunk_depth": 2,
        "l2_level_exclusive": True,
        "worker_count": 1,
    }
    arguments[field] = value

    with pytest.raises(OverflowError, match=field):
        core.extract_python_repository_chunk_spans([], **arguments)


@pytest.mark.skipif(
    _native_batch_core() is None, reason="native repository batch POC not built"
)
@pytest.mark.parametrize("exclusive", [0, 1, None, "true", 1.0])
def test_real_binding_rejects_non_exact_l2_boolean(exclusive):
    core = _native_batch_core()
    with pytest.raises((TypeError, ValueError), match="l2_level_exclusive"):
        core.extract_python_repository_chunk_spans(
            [], chunk_depth=2, l2_level_exclusive=exclusive, worker_count=1
        )
