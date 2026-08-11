# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Parity, validation, and fallback tests for the Python chunk POC."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from codenib.code_chunking import python_chunker
from codenib.code_chunking.base import BaseCodeChunker
from codenib.code_chunking.python_chunker import PythonCodeChunker

EXPECTED_CONTRACT = {
    "abi_version": 1,
    "row_size": 20,
    "grammar": "tree-sitter-python@v0.25.0",
    "runtime": "tree-sitter@v0.25.2",
    "semantic_profile": "python-definition-spans-v1",
}

PARITY_SOURCES = [
    pytest.param(
        """\
# tête
@trace(α=1)
async def café(value: str):
    return value

@registered
class 服务:
    @classmethod
    async def 创建(cls):
        return cls()
""",
        id="decorators-async-unicode",
    ),
    pytest.param(
        """\
def identity[T](value: T) -> T:
    return value

class Box[T]:
    def get(self) -> T:
        ...
""",
        id="pep695-generics",
    ),
    pytest.param(
        """\
def outer():
    def inner():
        return 1
    return inner()

class Visible:
    class Nested:
        def hidden(self):
            return 2

    def direct(self):
        return 3
""",
        id="nested-definitions",
    ),
    pytest.param(
        """\
if ENABLED:
    def conditional():
        return 1

class Service:
    if ENABLED:
        def conditional_method(self):
            return 2

    def direct(self):
        return 3
""",
        id="conditional-definitions",
    ),
    pytest.param(
        """\
def valid():
    return 1

def broken(:
    missing

class Recovered:
    def method(self):
        return 2
""",
        id="error-recovery",
    ),
    pytest.param(
        """\
@first
@second(
    1,
)
def multiline(
    value,
):
    return value
""",
        id="multiline-decorators",
    ),
    pytest.param(
        """\
# module header
import os

def body():
    return os.name

# module epilogue
RESULT = body()
""",
        id="header-epilogue",
    ),
    pytest.param(
        "def compact(): return 1\nclass Empty: pass\n",
        id="single-line-suites",
    ),
    pytest.param("", id="empty-source"),
    pytest.param(
        """\
@decorated
class Factory:
    @classmethod
    async def build(cls):
        return cls()

    def run(self):
        return None
""",
        id="decorated-class-methods",
    ),
]

PARITY_CONFIGURATIONS = [
    pytest.param(
        {
            "chunk_depth": 1,
            "l2_level_exclusive": True,
            "include_header_epilogue": False,
            "max_lines_per_chunk": None,
        },
        id="l1",
    ),
    pytest.param(
        {
            "chunk_depth": 2,
            "l2_level_exclusive": True,
            "include_header_epilogue": False,
            "max_lines_per_chunk": None,
        },
        id="l2-exclusive",
    ),
    pytest.param(
        {
            "chunk_depth": 2,
            "l2_level_exclusive": False,
            "include_header_epilogue": True,
            "max_lines_per_chunk": 3,
        },
        id="l2-containers-headers-split",
    ),
]


def _native_available() -> bool:
    core = python_chunker._load_native_core()
    return core is not None and hasattr(core, "extract_python_chunk_spans")


def _payload(*, rows=b"", arena=b"", row_count=0, **overrides):
    payload = {
        "abi_version": 1,
        "row_count": row_count,
        "rows": rows,
        "arena": arena,
        "parse_ns": 2_000_000,
        "extract_ns": 1_000_000,
    }
    payload.update(overrides)
    return payload


def test_validate_native_contract_requires_exact_profile():
    class Core:
        @staticmethod
        def python_chunk_poc_contract():
            return dict(EXPECTED_CONTRACT)

    assert python_chunker._validate_native_contract(Core()) == EXPECTED_CONTRACT


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("abi_version", True),
        ("abi_version", 2),
        ("row_size", True),
        ("row_size", 24),
        ("grammar", "tree-sitter-python@v0.24.0"),
        ("runtime", "tree-sitter@v0.24.0"),
        ("semantic_profile", "python-definition-spans-v2"),
        ("semantic_profile", None),
    ],
)
def test_validate_native_contract_rejects_mismatch(field, value):
    contract = dict(EXPECTED_CONTRACT)
    contract[field] = value

    class Core:
        @staticmethod
        def python_chunk_poc_contract():
            return contract

    with pytest.raises(RuntimeError, match=field):
        python_chunker._validate_native_contract(Core())


def test_decode_native_chunk_payload_reads_compact_rows():
    rows = b"".join(
        [
            python_chunker._NATIVE_CHUNK_ROW.pack(1, 3, 1, 0, 3),
            python_chunker._NATIVE_CHUNK_ROW.pack(5, 8, 3, 3, 9),
        ]
    )
    definitions, timings = python_chunker._decode_native_chunk_payload(
        _payload(rows=rows, arena=b"topThing.run", row_count=2),
        source_line_count=10,
    )

    assert [
        (node.start_point[0], node.end_point[0], name, kind)
        for node, name, kind in definitions
    ] == [
        (1, 3, "top", "function"),
        (5, 8, "Thing.run", "method"),
    ]
    assert timings == {
        "native_parse": 0.002,
        "native_extract_encode": 0.001,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        _payload(abi_version=True),
        _payload(abi_version=2),
        _payload(row_count=True),
        _payload(row_count=1),
        _payload(
            rows=python_chunker._NATIVE_CHUNK_ROW.pack(4, 3, 1, 0, 1),
            arena=b"x",
            row_count=1,
        ),
        _payload(
            rows=python_chunker._NATIVE_CHUNK_ROW.pack(9, 10, 1, 0, 1),
            arena=b"x",
            row_count=1,
        ),
        _payload(
            rows=b"".join(
                [
                    python_chunker._NATIVE_CHUNK_ROW.pack(5, 6, 1, 0, 1),
                    python_chunker._NATIVE_CHUNK_ROW.pack(1, 2, 1, 1, 1),
                ]
            ),
            arena=b"xy",
            row_count=2,
        ),
        _payload(
            rows=python_chunker._NATIVE_CHUNK_ROW.pack(1, 2, 9, 0, 1),
            arena=b"x",
            row_count=1,
        ),
        _payload(
            rows=python_chunker._NATIVE_CHUNK_ROW.pack(1, 2, 1, 1, 2),
            arena=b"x",
            row_count=1,
        ),
        _payload(
            rows=python_chunker._NATIVE_CHUNK_ROW.pack(1, 2, 1, 0, 1),
            arena=b"\xff",
            row_count=1,
        ),
        _payload(parse_ns=True),
        _payload(extract_ns=-1),
        _payload(parse_ns=None),
    ],
)
def test_decode_native_chunk_payload_rejects_invalid_envelopes(payload):
    with pytest.raises((TypeError, ValueError)):
        python_chunker._decode_native_chunk_payload(payload, source_line_count=10)


@pytest.mark.parametrize("source_line_count", [True, 0, -1])
def test_decode_native_chunk_payload_rejects_invalid_source_line_count(
    source_line_count,
):
    with pytest.raises(ValueError, match="source_line_count"):
        python_chunker._decode_native_chunk_payload(
            _payload(), source_line_count=source_line_count
        )


def test_native_python_chunk_mode_defaults_off_without_loading_core(
    tmp_path, monkeypatch
):
    source = tmp_path / "sample.py"
    source.write_text("def established():\n    return 1\n", encoding="utf-8")
    monkeypatch.delenv("CODENIB_NATIVE_PYTHON_CHUNKER", raising=False)
    monkeypatch.setattr(
        python_chunker,
        "_load_native_core",
        lambda: pytest.fail("default-off route loaded the native module"),
    )

    chunker = PythonCodeChunker(chunk_depth=1)
    chunks = chunker.chunk_file(str(source), relative_path="sample.py")

    assert [chunk.name for chunk in chunks] == ["established"]
    assert chunker.native_chunk_backend == "python"
    assert chunker.native_chunk_timings == {}


def test_native_python_chunk_auto_mode_falls_back_per_file(tmp_path, monkeypatch):
    source = tmp_path / "sample.py"
    source.write_text("def established():\n    return 1\n", encoding="utf-8")
    chunker = PythonCodeChunker(chunk_depth=1)
    monkeypatch.setenv("CODENIB_NATIVE_PYTHON_CHUNKER", "off")
    reference = chunker.chunk_file(str(source), relative_path="sample.py")

    class BrokenCore:
        @staticmethod
        def python_chunk_poc_contract():
            return dict(EXPECTED_CONTRACT)

        @staticmethod
        def extract_python_chunk_spans(*args, **kwargs):
            raise RuntimeError("synthetic native failure")

    monkeypatch.setattr(python_chunker, "_load_native_core", lambda: BrokenCore())
    monkeypatch.setenv("CODENIB_NATIVE_PYTHON_CHUNKER", "auto")

    assert chunker.chunk_file(str(source), relative_path="sample.py") == reference
    assert chunker.native_chunk_backend == "python-fallback"
    assert "synthetic native failure" in chunker.native_chunk_fallback_error


def test_native_python_chunk_auto_mode_falls_back_on_contract_mismatch(
    tmp_path, monkeypatch
):
    source = tmp_path / "sample.py"
    source.write_text("def established():\n    return 1\n", encoding="utf-8")
    chunker = PythonCodeChunker(chunk_depth=1)
    monkeypatch.setenv("CODENIB_NATIVE_PYTHON_CHUNKER", "off")
    reference = chunker.chunk_file(str(source), relative_path="sample.py")

    class WrongCore:
        @staticmethod
        def python_chunk_poc_contract():
            return {**EXPECTED_CONTRACT, "semantic_profile": "unexpected"}

        @staticmethod
        def extract_python_chunk_spans(*args, **kwargs):
            pytest.fail("mismatched contract reached native extraction")

    monkeypatch.setattr(python_chunker, "_load_native_core", lambda: WrongCore())
    monkeypatch.setenv("CODENIB_NATIVE_PYTHON_CHUNKER", "auto")

    assert chunker.chunk_file(str(source), relative_path="sample.py") == reference
    assert chunker.native_chunk_backend == "python-fallback"
    assert "semantic_profile" in chunker.native_chunk_fallback_error


def test_native_python_chunk_required_mode_fails_closed_without_api(
    tmp_path, monkeypatch
):
    source = tmp_path / "sample.py"
    source.write_text("def established():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(python_chunker, "_load_native_core", object)
    monkeypatch.setenv("CODENIB_NATIVE_PYTHON_CHUNKER", "required")

    with pytest.raises(RuntimeError, match="does not include"):
        PythonCodeChunker(chunk_depth=1).chunk_file(str(source))


def test_native_python_chunk_required_mode_fails_closed_on_malformed_payload(
    tmp_path, monkeypatch
):
    source = tmp_path / "sample.py"
    source.write_text("def established():\n    return 1\n", encoding="utf-8")

    class MalformedCore:
        @staticmethod
        def python_chunk_poc_contract():
            return dict(EXPECTED_CONTRACT)

        @staticmethod
        def extract_python_chunk_spans(*args, **kwargs):
            rows = python_chunker._NATIVE_CHUNK_ROW.pack(0, 100, 1, 0, 11)
            return _payload(rows=rows, arena=b"established", row_count=1)

    monkeypatch.setattr(python_chunker, "_load_native_core", lambda: MalformedCore())
    monkeypatch.setenv("CODENIB_NATIVE_PYTHON_CHUNKER", "required")

    with pytest.raises(ValueError, match="source line bounds"):
        PythonCodeChunker(chunk_depth=1).chunk_file(str(source))


def test_native_python_chunk_required_mode_rejects_unsupported_skeleton(
    tmp_path, monkeypatch
):
    source = tmp_path / "sample.py"
    source.write_text("def established():\n    return 1\n", encoding="utf-8")
    chunker = PythonCodeChunker(chunk_depth=2, skeleton_mode=True)
    monkeypatch.setenv("CODENIB_NATIVE_PYTHON_CHUNKER", "required")

    with pytest.raises(RuntimeError, match="non-skeleton"):
        chunker.chunk_file(str(source))


def test_native_python_chunk_mode_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("CODENIB_NATIVE_PYTHON_CHUNKER", "sometimes")

    with pytest.raises(ValueError, match="must be auto"):
        python_chunker._native_chunk_mode()


@pytest.mark.skipif(not _native_available(), reason="native chunk POC not built")
@pytest.mark.parametrize("configuration", PARITY_CONFIGURATIONS)
@pytest.mark.parametrize("source_text", PARITY_SOURCES)
def test_native_python_chunks_match_existing_chunker(
    tmp_path, monkeypatch, configuration, source_text
):
    source = tmp_path / "sample.py"
    source.write_text(source_text, encoding="utf-8")
    chunker = PythonCodeChunker(**configuration)

    monkeypatch.setenv("CODENIB_NATIVE_PYTHON_CHUNKER", "off")
    reference = chunker.chunk_file(str(source), relative_path="pkg/sample.py")
    monkeypatch.setenv("CODENIB_NATIVE_PYTHON_CHUNKER", "required")
    candidate = chunker.chunk_file(str(source), relative_path="pkg/sample.py")

    assert candidate == reference
    assert chunker.native_chunk_backend == "native-tree-sitter-poc"
    assert chunker.native_chunk_fallback_error is None
    assert chunker.native_chunk_timings["native_parse"] >= 0


@pytest.mark.skipif(not _native_available(), reason="native chunk POC not built")
def test_native_and_legacy_routes_share_source_reader(tmp_path, monkeypatch):
    source = tmp_path / "sample.py"
    source.write_text("def shared():\n    return 1\n", encoding="utf-8")
    calls = []
    original = BaseCodeChunker._read_source

    def tracked_reader(file_path):
        calls.append(file_path)
        return original(file_path)

    monkeypatch.setattr(BaseCodeChunker, "_read_source", staticmethod(tracked_reader))
    chunker = PythonCodeChunker(chunk_depth=1)
    monkeypatch.setenv("CODENIB_NATIVE_PYTHON_CHUNKER", "off")
    reference = chunker.chunk_file(str(source), relative_path="sample.py")
    monkeypatch.setenv("CODENIB_NATIVE_PYTHON_CHUNKER", "required")
    candidate = chunker.chunk_file(str(source), relative_path="sample.py")

    assert candidate == reference
    assert calls == [str(source), str(source)]


@pytest.mark.skipif(not _native_available(), reason="native chunk POC not built")
@pytest.mark.parametrize("chunk_depth", [0, 3, -1])
def test_native_binding_rejects_unsupported_chunk_depth(chunk_depth):
    core = python_chunker._load_native_core()

    with pytest.raises(ValueError, match="chunk_depth"):
        core.extract_python_chunk_spans(
            "def sample():\n    return 1\n",
            chunk_depth=chunk_depth,
            l2_level_exclusive=True,
        )


@pytest.mark.skipif(not _native_available(), reason="native chunk POC not built")
def test_native_binding_is_deterministic_across_threads():
    core = python_chunker._load_native_core()
    source = """\
@trace
async def café(value):
    return value

class Service:
    def run(self):
        return café(1)
"""

    def extract_semantics(_):
        payload = core.extract_python_chunk_spans(
            source, chunk_depth=2, l2_level_exclusive=False
        )
        return (
            payload["abi_version"],
            payload["row_count"],
            payload["rows"],
            payload["arena"],
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(extract_semantics, range(32)))

    assert results
    assert all(result == results[0] for result in results)
    assert python_chunker._validate_native_contract(core) == EXPECTED_CONTRACT
