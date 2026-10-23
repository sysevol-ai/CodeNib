# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Go graphs cover nested modules and test files."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from google.protobuf.text_format import MessageToString

from codenib.scip_interface import scip_indexer_go as go
from codenib.scip_interface.scip_decode_go import SCIPGoGraphDecoder
from codenib.scip_interface.scip_pb2 import Index

ROOT = "example.test/repo"
SERVER = "example.test/repo/server/v2"


def _symbol(module: str, package: str, descriptor: str) -> str:
    return f"scip-go gomod {module} v0 `{package}`/{descriptor}"


def _document(index: Index, path: str, occurrences) -> None:
    document = index.documents.add(relative_path=path)
    for line, symbol, roles in occurrences:
        document.occurrences.add(range=[line, 0, 4], symbol=symbol, symbol_roles=roles)


def _root_index() -> Index:
    index = Index()
    _document(index, "lib/lib.go", [(2, _symbol(ROOT, f"{ROOT}/lib", "Helper()."), 1)])
    _document(
        index,
        "lib/lib_test.go",
        [
            (4, _symbol(ROOT, f"{ROOT}/lib", "TestHelper()."), 1),
            (5, _symbol(ROOT, f"{ROOT}/lib", "Helper()."), 0),
        ],
    )
    return index


def _server_index() -> Index:
    index = Index()
    _document(
        index,
        "auth/auth.go",
        [
            (3, _symbol(SERVER, f"{SERVER}/auth", "Check()."), 1),
            (4, _symbol(ROOT, f"{ROOT}/lib", "Helper()."), 0),
        ],
    )
    index.external_symbols.add(symbol=_symbol(ROOT, f"{ROOT}/lib", "Helper()."))
    return index


def _write_modules(root: Path) -> None:
    for directory, module in [
        (".", ROOT),
        ("server", SERVER),
        ("server/vendor/dep", "example.test/dep"),
        ("lib/testdata/fixture", "example.test/fixture"),
        (".github/tools", "example.test/ci"),
        ("_examples/demo", "example.test/demo"),
        ("tools/gen", "example.test/repo/tools/gen"),
    ]:
        (root / directory).mkdir(parents=True, exist_ok=True)
        (root / directory / "go.mod").write_text(
            f"// generated\nmodule {module}\n\ngo 1.22\n", encoding="utf-8"
        )


def test_nested_modules_skip_vendored_testdata_and_hidden_trees(tmp_path):
    _write_modules(tmp_path)
    assert go.nested_go_modules(tmp_path) == [
        (SERVER, "server"),
        ("example.test/repo/tools/gen", "tools/gen"),
    ]
    assert go.go_module_path(tmp_path / "go.mod") == ROOT


def test_rename_moves_nested_packages_under_the_root_module():
    directories = {SERVER: "server"}
    rename = go.rename_module_symbol
    assert rename(_symbol(SERVER, f"{SERVER}/auth", "Check()."), ROOT, directories) == (
        _symbol(ROOT, f"{ROOT}/server/auth", "Check().")
    )
    assert rename(_symbol(SERVER, SERVER, "Run()."), ROOT, directories) == (
        _symbol(ROOT, f"{ROOT}/server", "Run().")
    )
    for unchanged in (
        _symbol(ROOT, f"{ROOT}/lib", "Helper()."),
        _symbol("golang.org/x/sync", "golang.org/x/sync/errgroup", "Group#"),
        "local 3",
    ):
        assert rename(unchanged, ROOT, directories) == unchanged


def _decode(tmp_path: Path, index: Index):
    (tmp_path / "go.mod").write_text(f"module {ROOT}\n", encoding="utf-8")
    decoded = tmp_path / "index.decoded"
    decoded.write_text(MessageToString(index, as_utf8=True), encoding="utf-8")
    return SCIPGoGraphDecoder(str(decoded), project_root=str(tmp_path)).decode()


def test_merged_graph_names_packages_by_directory_and_links_modules(tmp_path):
    merged = go.merge_module_indexes(
        _root_index(), [("server", _server_index())], ROOT, {SERVER: "server"}
    )
    assert [d.relative_path for d in merged.documents] == [
        "lib/lib.go",
        "lib/lib_test.go",
        "server/auth/auth.go",
    ]
    assert len(merged.external_symbols) == 1
    graph = _decode(tmp_path, merged)
    names = graph.name_to_vertex
    assert {"lib/Helper", "lib/TestHelper", "server/auth/Check"} <= set(names)
    assert graph.graph.vs[names["lib/lib_test.go"]]["type"] == "file"
    references = {
        (graph.graph.vs[e.source]["name"], graph.graph.vs[e.target]["name"])
        for e in graph.graph.es
        if e["type"] == "reference"
    }
    # Without enclosing ranges in this fixture, references start at the file.
    assert ("server/auth/auth.go", "lib/Helper") in references
    assert ("lib/lib_test.go", "lib/Helper") in references


def test_generate_index_merges_modules_and_skips_failures(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    _write_modules(repository)
    indexer = go.SCIPGoIndexer(repository, output_dir=tmp_path / "state")
    monkeypatch.setattr(indexer, "_check_indexer_available", lambda: True)
    calls = []

    def run(command, **kwargs):
        cwd = Path(kwargs["cwd"])
        calls.append(cwd.relative_to(repository).as_posix())
        output = Path(command[command.index("--output") + 1])
        if cwd == repository / "tools/gen":
            raise subprocess.CalledProcessError(1, command)
        index = _root_index() if cwd == repository else _server_index()
        output.write_bytes(index.SerializeToString())
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert indexer.generate_index() is True
    assert calls == [".", "server", "tools/gen"]
    merged = Index()
    merged.ParseFromString(indexer.index_file.read_bytes())
    assert merged.documents[-1].relative_path == "server/auth/auth.go"
    assert merged.documents[-1].occurrences[0].symbol == _symbol(
        ROOT, f"{ROOT}/server/auth", "Check()."
    )
    assert sorted(p.name for p in indexer.output_dir.iterdir()) == ["index.scip"]


def test_single_module_repository_runs_scip_go_once(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "go.mod").write_text(f"module {ROOT}\n", encoding="utf-8")
    indexer = go.SCIPGoIndexer(repository, output_dir=tmp_path / "state")
    monkeypatch.setattr(indexer, "_check_indexer_available", lambda: True)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        indexer.index_file.write_bytes(_root_index().SerializeToString())
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert indexer.generate_index() is True
    assert calls == [["scip-go", "--output", str(indexer.index_file)]]


@pytest.mark.parametrize("path", ["lib/lib_test.go"])
def test_go_test_files_are_decoded(tmp_path, path):
    graph = _decode(tmp_path, _root_index())
    assert path in graph.name_to_vertex
