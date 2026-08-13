#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Private repository-batch adapter for the native Python chunking POC.

This module is deliberately not imported by the production chunker package.  It
keeps the experimental repository boundary behind one explicit function while
reusing the established repository selector, source decoder, and Python chunk
materializer.
"""

from __future__ import annotations

import math
import os
import time
from collections import namedtuple
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from struct import Struct
from typing import Any, Iterator, Mapping, Sequence

from ..code_chunker import CodeChunker, RepoChunkingConfig
from .base import CodeChunk

_BATCH_MODE_ENV = "CODENIB_NATIVE_PYTHON_CHUNK_BATCH"
_PER_FILE_MODE_ENV = "CODENIB_NATIVE_PYTHON_CHUNKER"
_MODES = frozenset({"off", "auto", "required"})
_SUPPORTED_WORKER_COUNTS = (1, 2, 4)
_BACKEND = "native-repository-batch-poc"

_FILE_ROW = Struct("<II")
_SPAN_ROW = Struct("<IIB3xII")
_KINDS = {1: "function", 2: "class", 3: "method"}
_NativeNodeSpan = namedtuple("_NativeNodeSpan", ["start_point", "end_point"])

_CONTRACT_STATIC = {
    "abi_version": 1,
    "file_row_size": _FILE_ROW.size,
    "file_row_format": "<II",
    "span_row_size": _SPAN_ROW.size,
    "span_row_format": "<IIB3xII",
    "grammar": "tree-sitter-python@v0.25.0",
    "runtime": "tree-sitter@v0.25.2",
    "semantic_profile": "python-definition-spans-v1",
    "ordering": "input-ordinal-then-span-order-v1",
    "supported_worker_counts": list(_SUPPORTED_WORKER_COUNTS),
    "overflow_policy": "fail-closed-v1",
}
_CONTRACT_CAPS = {
    "max_file_count": 10_000,
    "max_source_bytes": 1_000_000_000,
    "max_row_count": 10_000_000,
    "max_arena_bytes": 1_000_000_000,
}
_EXPECTED_CONTRACT = {**_CONTRACT_STATIC, **_CONTRACT_CAPS}

_PAYLOAD_KEYS = {
    "abi_version",
    "file_count",
    "source_bytes",
    "row_count",
    "worker_count",
    "file_rows",
    "rows",
    "arena",
    "batch_wall_ns",
    "parse_work_ns",
    "extract_encode_work_ns",
    "ordered_merge_ns",
}
_STAGE_FIELDS = (
    "discovery_read_wall_seconds",
    "binding_wall_seconds",
    "native_batch_wall_seconds",
    "native_parse_work_seconds",
    "native_extract_encode_work_seconds",
    "native_ordered_merge_seconds",
    "buffer_decode_wall_seconds",
    "chunk_materialization_wall_seconds",
    "node_materialization_wall_seconds",
)


@dataclass(frozen=True)
class RepositoryChunkBatchResult:
    """Completely materialized result returned by the private batch adapter."""

    chunks: list[CodeChunk]
    nodes: list[str]
    diagnostics: dict[str, Any]


@dataclass(frozen=True)
class _AdapterConfig:
    chunk_depth: int
    l2_level_exclusive: bool
    skeleton_mode: bool
    include_header_epilogue: bool
    max_lines_per_chunk: int | None
    filter_tests: bool
    strict: bool


@dataclass
class _AttemptState:
    batch_call_count: int = 0
    contract: dict[str, Any] | None = None
    stages: dict[str, float] | None = None


@dataclass(frozen=True)
class _SelectedSource:
    path: Path
    relative_path: str
    content: str
    raw_size: int
    encoded_size: int


def _zero_stages() -> dict[str, float]:
    return {field: 0.0 for field in _STAGE_FIELDS}


def _resolve_mode(mode: str | None) -> str:
    value = os.environ.get(_BATCH_MODE_ENV, "off") if mode is None else mode
    if type(value) is not str:
        raise TypeError("repository batch mode must be a string")
    normalized = value.strip().lower()
    if normalized not in _MODES:
        raise ValueError(
            f"{_BATCH_MODE_ENV} must be off, auto, or required; got {value!r}"
        )
    return normalized


def _validate_worker_count(worker_count: object) -> int:
    if type(worker_count) is not int or worker_count not in _SUPPORTED_WORKER_COUNTS:
        raise ValueError("worker_count must be exactly one of 1, 2, or 4")
    return worker_count


def _require_bool(config: Mapping[str, Any], field: str) -> bool:
    value = config.get(field)
    if type(value) is not bool:
        raise TypeError(f"repository batch config {field} must be a bool")
    return value


def _validate_config(config: Mapping[str, Any]) -> _AdapterConfig:
    if not isinstance(config, Mapping):
        raise TypeError("repository batch config must be a mapping")
    if config.get("language") != "python":
        raise ValueError("repository batch POC supports only the Python language")

    chunk_depth = config.get("chunk_depth")
    if type(chunk_depth) is not int:
        raise TypeError("repository batch config chunk_depth must be an integer")
    max_lines = config.get("max_lines_per_chunk")
    if max_lines is not None and (type(max_lines) is not int or max_lines <= 0):
        raise ValueError(
            "repository batch config max_lines_per_chunk must be positive or None"
        )
    policy = config.get("repository_filter_policy", 3)
    if type(policy) is not int or policy != 3:
        raise ValueError("repository batch POC requires repository filter policy 3")

    return _AdapterConfig(
        chunk_depth=chunk_depth,
        l2_level_exclusive=_require_bool(config, "l2_level_exclusive"),
        skeleton_mode=_require_bool(config, "skeleton_mode"),
        include_header_epilogue=_require_bool(config, "include_header_epilogue"),
        max_lines_per_chunk=max_lines,
        filter_tests=_require_bool(config, "filter_tests"),
        strict=_require_bool(config, "strict"),
    )


def _new_chunker(config: _AdapterConfig) -> CodeChunker:
    return CodeChunker(
        language="python",
        repo_config=RepoChunkingConfig(
            languages=["python"],
            filter_tests=config.filter_tests,
        ),
        max_lines_per_chunk=config.max_lines_per_chunk,
        chunk_depth=config.chunk_depth,
        include_header_epilogue=config.include_header_epilogue,
        l2_level_exclusive=config.l2_level_exclusive,
        skeleton_mode=config.skeleton_mode,
    )


def _resolve_repository(repo_root: str | os.PathLike[str]) -> Path:
    root = Path(repo_root).resolve()
    if not root.exists():
        raise ValueError(f"Repository path does not exist: {root}")
    if not root.is_dir():
        raise ValueError(f"Repository path is not a directory: {root}")
    return root


def _load_native_core() -> Any | None:
    try:
        import codenib_core
    except ImportError:
        return None
    return codenib_core


def _validate_native_contract(core: Any) -> dict[str, Any]:
    contract_function = getattr(core, "python_chunk_batch_poc_contract", None)
    extraction_function = getattr(core, "extract_python_repository_chunk_spans", None)
    if not callable(contract_function) or not callable(extraction_function):
        raise RuntimeError("compiled core does not include repository batch POC APIs")
    contract = contract_function()
    if not isinstance(contract, Mapping):
        raise TypeError("native repository batch contract must be a mapping")
    payload = dict(contract)
    if set(payload) != set(_EXPECTED_CONTRACT):
        raise RuntimeError("native repository batch contract schema changed")
    for field, expected in _EXPECTED_CONTRACT.items():
        observed = payload[field]
        if type(observed) is not type(expected) or observed != expected:
            raise RuntimeError(
                f"native repository batch {field} mismatch: "
                f"compiled={observed!r}, python={expected!r}"
            )
    return payload


def _exact_nonnegative_int(value: object, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"native repository batch {label} must be a non-negative int")
    return value


def _nanoseconds_to_seconds(value: object, *, label: str) -> float:
    nanoseconds = _exact_nonnegative_int(value, label=label)
    seconds = nanoseconds / 1_000_000_000
    if not math.isfinite(seconds):
        raise ValueError(f"native repository batch {label} is not finite")
    return seconds


def _utf8_size(source: str) -> int:
    # Python source is overwhelmingly ASCII.  Avoid allocating a second full
    # encoded copy on that path while retaining exact byte accounting for
    # Unicode and UTF-8 replacement characters.
    return len(source) if source.isascii() else len(source.encode("utf-8"))


def _validate_pre_call_caps(
    *, file_count: int, source_bytes: int, contract: Mapping[str, Any]
) -> None:
    if file_count > contract["max_file_count"]:
        raise ValueError("native repository batch file count exceeds contract cap")
    if source_bytes > contract["max_source_bytes"]:
        raise ValueError("native repository batch source bytes exceed contract cap")


def _decode_native_batch_payload(
    payload: Any,
    *,
    sources: Sequence[str],
    requested_worker_count: int,
    contract: Mapping[str, Any],
    expected_source_bytes: int | None = None,
) -> tuple[list[list[tuple[Any, str, str]]], dict[str, float], int]:
    """Validate and decode one complete native batch before materialization."""

    if not isinstance(payload, Mapping):
        raise TypeError("native repository batch payload must be a mapping")
    raw = dict(payload)
    if set(raw) != _PAYLOAD_KEYS:
        raise ValueError("native repository batch payload schema changed")

    abi_version = raw["abi_version"]
    if type(abi_version) is not int or abi_version != contract["abi_version"]:
        raise ValueError("native repository batch payload ABI mismatch")
    file_count = _exact_nonnegative_int(raw["file_count"], label="file_count")
    source_bytes = _exact_nonnegative_int(raw["source_bytes"], label="source_bytes")
    row_count = _exact_nonnegative_int(raw["row_count"], label="row_count")
    worker_count = _exact_nonnegative_int(raw["worker_count"], label="worker_count")
    if file_count != len(sources):
        raise ValueError("native repository batch file count mismatch")
    if file_count > contract["max_file_count"]:
        raise ValueError("native repository batch file count exceeds contract cap")
    if expected_source_bytes is None:
        expected_source_bytes = sum(_utf8_size(source) for source in sources)
    elif type(expected_source_bytes) is not int or expected_source_bytes < 0:
        raise ValueError("expected source bytes must be a non-negative integer")
    if source_bytes != expected_source_bytes:
        raise ValueError("native repository batch source byte count mismatch")
    if source_bytes > contract["max_source_bytes"]:
        raise ValueError("native repository batch source bytes exceed contract cap")
    if row_count > contract["max_row_count"]:
        raise ValueError("native repository batch row count exceeds contract cap")
    if worker_count != requested_worker_count:
        raise ValueError("native repository batch actual worker count mismatch")

    file_rows = raw["file_rows"]
    rows = raw["rows"]
    arena = raw["arena"]
    if (
        type(file_rows) is not bytes
        or type(rows) is not bytes
        or type(arena) is not bytes
    ):
        raise TypeError("native repository batch tables and arena must be bytes")
    if len(file_rows) != file_count * contract["file_row_size"]:
        raise ValueError("native repository batch file table has an invalid size")
    if len(rows) != row_count * contract["span_row_size"]:
        raise ValueError("native repository batch span table has an invalid size")
    if len(arena) > contract["max_arena_bytes"]:
        raise ValueError("native repository batch name arena exceeds contract cap")
    try:
        arena.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            "native repository batch name arena is not valid UTF-8"
        ) from exc

    file_segments: list[tuple[int, int]] = []
    next_row = 0
    for file_ordinal in range(file_count):
        row_offset, file_row_count = _FILE_ROW.unpack_from(
            file_rows, file_ordinal * _FILE_ROW.size
        )
        if row_offset != next_row:
            raise ValueError(
                "native repository batch file rows contain a gap or overlap"
            )
        row_end = row_offset + file_row_count
        if row_end > row_count:
            raise ValueError("native repository batch file rows exceed the span table")
        file_segments.append((row_offset, row_end))
        next_row = row_end
    if next_row != row_count:
        raise ValueError("native repository batch file rows do not cover all spans")

    decoded: list[list[tuple[Any, str, str]]] = []
    for (row_start, row_end), source in zip(file_segments, sources, strict=True):
        line_count = len(source.split("\n"))
        definitions: list[tuple[Any, str, str]] = []
        previous_start: int | None = None
        for row_ordinal in range(row_start, row_end):
            row_byte_offset = row_ordinal * _SPAN_ROW.size
            if rows[row_byte_offset + 9 : row_byte_offset + 12] != b"\0\0\0":
                raise ValueError("native repository batch span padding is nonzero")
            start_line, end_line, kind_id, name_offset, name_length = (
                _SPAN_ROW.unpack_from(rows, row_byte_offset)
            )
            if start_line > end_line:
                raise ValueError("native repository batch span is reversed")
            if end_line >= line_count:
                raise ValueError(
                    "native repository batch span exceeds source line bounds"
                )
            if previous_start is not None and start_line < previous_start:
                raise ValueError("native repository batch file spans are not sorted")
            previous_start = start_line
            kind = _KINDS.get(kind_id)
            if kind is None:
                raise ValueError(f"native repository batch kind is invalid: {kind_id}")
            name_end = name_offset + name_length
            if name_offset > len(arena) or name_end > len(arena):
                raise ValueError("native repository batch name exceeds arena bounds")
            try:
                name = arena[name_offset:name_end].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    "native repository batch name is not valid UTF-8"
                ) from exc
            if not name:
                raise ValueError("native repository batch name must not be empty")
            node = _NativeNodeSpan((start_line, 0), (end_line, 0))
            definitions.append((node, name, kind))
        decoded.append(definitions)

    batch_wall_ns = _exact_nonnegative_int(raw["batch_wall_ns"], label="batch_wall_ns")
    ordered_merge_ns = _exact_nonnegative_int(
        raw["ordered_merge_ns"], label="ordered_merge_ns"
    )
    if ordered_merge_ns > batch_wall_ns:
        raise ValueError("native repository batch ordered merge exceeds batch wall")
    telemetry = {
        "native_batch_wall_seconds": batch_wall_ns / 1_000_000_000,
        "native_parse_work_seconds": _nanoseconds_to_seconds(
            raw["parse_work_ns"], label="parse_work_ns"
        ),
        "native_extract_encode_work_seconds": _nanoseconds_to_seconds(
            raw["extract_encode_work_ns"], label="extract_encode_work_ns"
        ),
        "native_ordered_merge_seconds": ordered_merge_ns / 1_000_000_000,
    }
    return decoded, telemetry, row_count


def _canonical_nodes(chunks: Sequence[CodeChunk]) -> list[str]:
    return sorted(
        {
            chunk.node_id
            for chunk in chunks
            if type(chunk.node_id) is str and chunk.node_id and ":" in chunk.node_id
        }
    )


@contextmanager
def _per_file_native_disabled() -> Iterator[None]:
    previous = os.environ.get(_PER_FILE_MODE_ENV)
    os.environ[_PER_FILE_MODE_ENV] = "off"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(_PER_FILE_MODE_ENV, None)
        else:
            os.environ[_PER_FILE_MODE_ENV] = previous


def _legacy_result(
    root: Path,
    config: _AdapterConfig,
    *,
    backend: str,
    fallback_count: int,
    batch_call_count: int,
    contract: dict[str, Any] | None,
    prior_stages: Mapping[str, float] | None = None,
) -> RepositoryChunkBatchResult:
    stages = _zero_stages()
    if prior_stages is not None:
        stages.update(prior_stages)
    started = time.perf_counter()
    chunker = _new_chunker(config)
    with _per_file_native_disabled():
        chunks = chunker.chunk_repository(str(root), strict=config.strict)
    try:
        selected = chunker._discover_files(root, ["python"])
        file_count = len(selected)
        raw_source_bytes = sum(path.stat().st_size for path, _ in selected)
    except MemoryError:
        raise
    except Exception:
        # Diagnostics must not turn a successful established consumer call into
        # a failure.  This conservative path matters only if the repository
        # changes while post-result counters are collected.
        represented_files = {chunk.file for chunk in chunks}
        file_count = len(represented_files)
        raw_source_bytes = 0
        for file_name in represented_files:
            try:
                raw_source_bytes += Path(file_name).stat().st_size
            except OSError:
                continue
    stages["discovery_read_wall_seconds"] += time.perf_counter() - started
    node_started = time.perf_counter()
    nodes = _canonical_nodes(chunks)
    stages["node_materialization_wall_seconds"] += time.perf_counter() - node_started
    diagnostics = {
        "backend": backend,
        "fallback_count": fallback_count,
        "batch_call_count": batch_call_count,
        "worker_count": None,
        "contract": contract,
        "counters": {
            "file_count": file_count,
            "source_bytes": raw_source_bytes,
            # The established consumer exposes materialized chunks, not its
            # pre-materialization definition-span count. Headers, epilogues,
            # and max-line splitting can turn one definition into several
            # chunks, so len(chunks) is not a truthful span count.
            "span_count": None,
            "chunk_count": len(chunks),
            "node_count": len(nodes),
        },
        "stages": stages,
    }
    return RepositoryChunkBatchResult(list(chunks), nodes, diagnostics)


def _read_selected_sources(
    chunker: CodeChunker, root: Path, config: _AdapterConfig
) -> list[_SelectedSource]:
    selected: list[_SelectedSource] = []
    for path, language in chunker._discover_files(root, ["python"]):
        if language != "python":
            raise RuntimeError(
                "repository batch discovery returned a non-Python source"
            )
        try:
            raw_size = path.stat().st_size
            content = chunker._chunker._read_source(str(path))
        except MemoryError:
            raise
        except Exception as exc:
            if config.strict:
                relative = path.relative_to(root).as_posix()
                raise RuntimeError(
                    f"Failed to chunk repository source {relative}: {exc}"
                ) from exc
            continue
        selected.append(
            _SelectedSource(
                path=path,
                relative_path=str(path.relative_to(root)),
                content=content,
                raw_size=raw_size,
                encoded_size=_utf8_size(content),
            )
        )
    return selected


def _native_result(
    root: Path,
    config: _AdapterConfig,
    worker_count: int,
    state: _AttemptState,
) -> RepositoryChunkBatchResult:
    if config.chunk_depth not in {1, 2} or config.skeleton_mode:
        raise RuntimeError(
            "native repository batch supports non-skeleton chunk_depth 1 or 2"
        )

    stages = _zero_stages()
    state.stages = stages
    discovery_started = time.perf_counter()
    try:
        chunker = _new_chunker(config)
        selected = _read_selected_sources(chunker, root, config)
    finally:
        stages["discovery_read_wall_seconds"] = time.perf_counter() - discovery_started
    sources = [item.content for item in selected]

    binding_started = time.perf_counter()
    try:
        core = _load_native_core()
        if core is None:
            raise RuntimeError(
                "compiled core does not include repository batch POC APIs"
            )
        contract = _validate_native_contract(core)
        state.contract = contract
        encoded_source_bytes = sum(item.encoded_size for item in selected)
        _validate_pre_call_caps(
            file_count=len(selected),
            source_bytes=encoded_source_bytes,
            contract=contract,
        )
        state.batch_call_count = 1
        payload = core.extract_python_repository_chunk_spans(
            sources,
            chunk_depth=config.chunk_depth,
            l2_level_exclusive=config.l2_level_exclusive,
            worker_count=worker_count,
        )
    finally:
        stages["binding_wall_seconds"] = time.perf_counter() - binding_started

    decode_started = time.perf_counter()
    try:
        definitions_by_file, native_stages, span_count = _decode_native_batch_payload(
            payload,
            sources=sources,
            requested_worker_count=worker_count,
            contract=contract,
            expected_source_bytes=encoded_source_bytes,
        )
        stages.update(native_stages)
    finally:
        stages["buffer_decode_wall_seconds"] = time.perf_counter() - decode_started

    materialize_started = time.perf_counter()
    try:
        chunks: list[CodeChunk] = []
        for item, definitions in zip(selected, definitions_by_file, strict=True):
            file_chunks = chunker._chunker._generate_chunks(
                lines=item.content.split("\n"),
                definitions=definitions,
                file_path=str(item.path),
                path_for_node_id=item.relative_path,
                code_content=item.content,
                skeleton_mode=False,
            )
            if type(file_chunks) is not list or any(
                type(chunk) is not CodeChunk for chunk in file_chunks
            ):
                raise TypeError("repository batch materializer returned invalid chunks")
            chunks.extend(file_chunks)
    finally:
        stages["chunk_materialization_wall_seconds"] = (
            time.perf_counter() - materialize_started
        )

    node_started = time.perf_counter()
    nodes = _canonical_nodes(chunks)
    stages["node_materialization_wall_seconds"] = time.perf_counter() - node_started
    raw_source_bytes = sum(item.raw_size for item in selected)
    diagnostics = {
        "backend": _BACKEND,
        "fallback_count": 0,
        "batch_call_count": 1,
        "worker_count": worker_count,
        "contract": contract,
        "counters": {
            "file_count": len(selected),
            "source_bytes": raw_source_bytes,
            "span_count": span_count,
            "chunk_count": len(chunks),
            "node_count": len(nodes),
        },
        "stages": stages,
    }
    return RepositoryChunkBatchResult(chunks, nodes, diagnostics)


def run_python_repository_batch(
    repo_root: str | os.PathLike[str],
    *,
    config: Mapping[str, Any],
    mode: str | None = None,
    worker_count: int = 1,
) -> RepositoryChunkBatchResult:
    """Chunk one Python repository through the private native batch boundary.

    ``off`` delegates to the established repository consumer. ``auto`` treats
    the candidate as one atomic attempt and reruns that entire consumer exactly
    once after an ordinary failure. ``required`` propagates candidate failures.
    Memory exhaustion is never converted into fallback work.
    """

    selected_mode = _resolve_mode(mode)
    workers = _validate_worker_count(worker_count)
    adapter_config = _validate_config(config)
    root = _resolve_repository(repo_root)

    if selected_mode == "off":
        return _legacy_result(
            root,
            adapter_config,
            backend="python",
            fallback_count=0,
            batch_call_count=0,
            contract=None,
        )

    state = _AttemptState()
    try:
        return _native_result(root, adapter_config, workers, state)
    except MemoryError:
        raise
    except Exception:
        if selected_mode == "required":
            raise
        return _legacy_result(
            root,
            adapter_config,
            backend="python-fallback",
            fallback_count=1,
            batch_call_count=state.batch_call_count,
            contract=state.contract,
            prior_stages=state.stages,
        )


__all__ = ["RepositoryChunkBatchResult", "run_python_repository_batch"]
