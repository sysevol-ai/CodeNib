# SPDX-FileCopyrightText: 2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import pytest

from codenib.cli import run
from codenib.parser_errors import (
    ParserUnavailableError,
    explain_parser_failure,
    library_arch,
)


def _macho(path: Path, cpu: int) -> Path:
    path.write_bytes(b"\xcf\xfa\xed\xfe" + cpu.to_bytes(4, "little") + b"\0" * 24)
    return path


def test_reads_the_architecture_of_a_thin_macho_library(tmp_path):
    assert library_arch(_macho(tmp_path / "a.dylib", 0x0100000C)) == "arm64"
    assert library_arch(_macho(tmp_path / "x.dylib", 0x01000007)) == "x86_64"
    (tmp_path / "elf.so").write_bytes(b"\x7fELF" + b"\0" * 12)
    assert library_arch(tmp_path / "elf.so") is None
    assert library_arch(tmp_path / "missing.dylib") is None


def test_names_rosetta_and_the_grammar_architecture(tmp_path):
    library = _macho(tmp_path / "libtree_sitter_python.dylib", 0x0100000C)
    reason = explain_parser_failure(
        "python",
        RuntimeError("Language 'python' not found"),
        machine="x86_64",
        rosetta=lambda: True,
        library=library,
    )
    assert "x86_64 under Rosetta" in reason
    assert "built for arm64" in reason
    assert "uv venv --python 3.12" in reason


def test_a_mismatched_grammar_is_named_without_rosetta(tmp_path):
    library = _macho(tmp_path / "libtree_sitter_go.dylib", 0x01000007)
    reason = explain_parser_failure(
        "go", RuntimeError("x"), machine="arm64", rosetta=lambda: False, library=library
    )
    assert "runs as arm64" in reason
    assert "built for x86_64" in reason
    assert "Rosetta" not in reason


def test_a_matching_machine_points_at_the_download(tmp_path):
    library = _macho(tmp_path / "libtree_sitter_go.dylib", 0x0100000C)
    reason = explain_parser_failure(
        "go",
        RuntimeError("Language 'go' not found"),
        machine="arm64",
        rosetta=lambda: False,
        library=library,
    )
    assert "Language 'go' not found" in reason
    assert "downloads each grammar" in reason


def test_the_chunker_raises_instead_of_exiting(monkeypatch):
    from codenib.code_chunking import base

    def missing(cls, language):
        raise RuntimeError(f"Language {language!r} not found")

    monkeypatch.setattr(
        base.BaseCodeChunker, "_get_tree_sitter_language", classmethod(missing)
    )
    monkeypatch.setattr("codenib.parser_errors.running_under_rosetta", lambda: False)
    from codenib.code_chunking.python_chunker import PythonCodeChunker

    with pytest.raises(ParserUnavailableError, match="could not load the python"):
        PythonCodeChunker()


def test_the_cli_prints_the_reason(monkeypatch, capsys):
    def boom(_args):
        raise ParserUnavailableError("CodeNib could not load the python parser")

    monkeypatch.setattr("codenib.cli.build_parser", lambda: _Parser(boom))
    assert run([]) == 2
    assert "could not load the python parser" in capsys.readouterr().err


class _Parser:
    def __init__(self, handler):
        self.handler = handler

    def parse_args(self, _argv):
        return type("Args", (), {"handler": staticmethod(self.handler)})()
