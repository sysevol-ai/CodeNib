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


def _symbol(module: str, package: str, descriptor: str, version: str = "v0") -> str:
    return f"scip-go gomod {module} {version} `{package}`/{descriptor}"


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
            # The root requires a released server version.
            (6, _symbol(SERVER, f"{SERVER}/auth", "Check().", "v2.1.0"), 0),
        ],
    )
    return index


def _server_index() -> Index:
    index = Index()
    _document(
        index,
        "auth/auth.go",
        [
            (3, _symbol(SERVER, f"{SERVER}/auth", "Check().", "abc123"), 1),
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
        # Indented directives are valid go.mod syntax.
        indent = "  " if directory == "tools/gen" else ""
        (root / directory / "go.mod").write_text(
            f"// generated\n{indent}module {module}\n\ngo 1.22\n", encoding="utf-8"
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
    assert rename(
        _symbol(SERVER, SERVER, "Run().", "v2.1.0"), ROOT, directories, "v0"
    ) == _symbol(ROOT, f"{ROOT}/server", "Run().")
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
        _root_index(), [(SERVER, "server", _server_index())], ROOT
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
    assert ("lib/lib_test.go", "server/auth/Check") in references
    # The root's reference to a released version and the checked-out
    # definition share one symbol, as occurrence lookups require.
    check = {
        occurrence.symbol
        for document in merged.documents
        for occurrence in document.occurrences
        if "Check()" in occurrence.symbol
    }
    assert check == {_symbol(ROOT, f"{ROOT}/server/auth", "Check().")}


def test_generate_index_merges_modules_and_skips_failures(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    _write_modules(repository)
    indexer = go.SCIPGoIndexer(repository, output_dir=tmp_path / "state")
    monkeypatch.setattr(indexer, "_check_indexer_available", lambda: True)
    calls = []

    def run(command, **kwargs):
        cwd = Path(kwargs["cwd"])
        calls.append(cwd.relative_to(repository).as_posix())
        # The root keeps any go.work; nested modules run without one.
        if cwd == repository:
            assert "env" not in kwargs
        else:
            assert kwargs["env"]["GOWORK"] == "off"
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
        assert "env" not in kwargs
        indexer.index_file.write_bytes(_root_index().SerializeToString())
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert indexer.generate_index() is True
    assert calls == [["scip-go", "--output", str(indexer.index_file)]]


@pytest.mark.parametrize("path", ["lib/lib_test.go"])
def test_go_test_files_are_decoded(tmp_path, path):
    graph = _decode(tmp_path, _root_index())
    assert path in graph.name_to_vertex


def test_versions_are_canonical_when_the_root_has_no_sources():
    client = "example.test/repo/client/v2"
    server = Index()
    _document(
        server,
        "auth/auth.go",
        [
            (3, _symbol(SERVER, f"{SERVER}/auth", "Check().", "abc123"), 1),
            (4, _symbol(client, f"{client}/api", "Call().", "v2.0.0"), 0),
        ],
    )
    client_index = Index()
    _document(
        client_index,
        "api/api.go",
        [(2, _symbol(client, f"{client}/api", "Call().", "abc123"), 1)],
    )
    merged = go.merge_module_indexes(
        Index(), [(SERVER, "server", server), (client, "client", client_index)], ROOT
    )
    calls = {
        occurrence.symbol
        for document in merged.documents
        for occurrence in document.occurrences
        if "Call()" in occurrence.symbol
    }
    assert calls == {_symbol(ROOT, f"{ROOT}/client/api", "Call().", "abc123")}


def test_duplicate_module_paths_keep_their_own_directories():
    dup = "example.test/dup"
    root = Index()
    _document(root, "main.go", [(1, _symbol(dup, dup, "Thing#"), 0)])

    def copy(line):
        index = Index()
        _document(index, "thing.go", [(line, _symbol(dup, dup, "Thing#"), 1)])
        return index

    merged = go.merge_module_indexes(
        root, [(dup, "examples/a", copy(1)), (dup, "examples/b", copy(2))], ROOT
    )
    symbols = [d.occurrences[0].symbol for d in merged.documents]
    assert symbols == [
        _symbol(dup, dup, "Thing#"),
        _symbol(ROOT, f"{ROOT}/examples/a", "Thing#"),
        _symbol(ROOT, f"{ROOT}/examples/b", "Thing#"),
    ]


def test_modules_the_root_run_already_indexed_are_not_rerun(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    _write_modules(repository)
    indexer = go.SCIPGoIndexer(repository, output_dir=tmp_path / "state")
    monkeypatch.setattr(indexer, "_check_indexer_available", lambda: True)
    calls = []

    def run(command, **kwargs):
        cwd = Path(kwargs["cwd"])
        calls.append(cwd.relative_to(repository).as_posix())
        index = _root_index()
        if cwd == repository:
            # A root go.work pulled the server module into ./...
            for document in _server_index().documents:
                document.relative_path = f"server/{document.relative_path}"
                index.documents.append(document)
        Path(command[command.index("--output") + 1]).write_bytes(
            index.SerializeToString()
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert indexer.generate_index() is True
    assert calls == [".", "tools/gen"]
    merged = Index()
    merged.ParseFromString(indexer.index_file.read_bytes())
    server = [d for d in merged.documents if d.relative_path.startswith("server/")]
    assert [d.relative_path for d in server] == ["server/auth/auth.go"]
    assert server[0].occurrences[0].symbol == _symbol(
        ROOT, f"{ROOT}/server/auth", "Check()."
    )


def test_nested_references_to_root_releases_take_the_root_version():
    server = Index()
    _document(
        server,
        "auth/auth.go",
        [(4, _symbol(ROOT, f"{ROOT}/lib", "Helper().", "v1.4.0"), 0)],
    )
    merged = go.merge_module_indexes(_root_index(), [(SERVER, "server", server)], ROOT)
    helpers = {
        occurrence.symbol
        for document in merged.documents
        for occurrence in document.occurrences
        if occurrence.symbol.endswith("/Helper().")
    }
    assert helpers == {_symbol(ROOT, f"{ROOT}/lib", "Helper().")}


def test_a_nested_copy_of_the_root_module_stays_under_its_directory():
    copy = Index()
    _document(copy, "lib/lib.go", [(2, _symbol(ROOT, f"{ROOT}/lib", "Helper()."), 1)])
    merged = go.merge_module_indexes(
        _root_index(), [(ROOT, "examples/copy", copy)], ROOT
    )
    definitions = [
        (document.relative_path, document.occurrences[0].symbol)
        for document in merged.documents
        if document.relative_path.endswith("lib.go")
    ]
    assert definitions == [
        ("lib/lib.go", _symbol(ROOT, f"{ROOT}/lib", "Helper().")),
        (
            "examples/copy/lib/lib.go",
            _symbol(ROOT, f"{ROOT}/examples/copy/lib", "Helper()."),
        ),
    ]


def test_local_replacements_resolve_inside_the_repository(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "go.mod").write_text(
        "module example.test/repo\n"
        "replace example.com/new => ./sub // renamed\n"
        "replace (\n"
        "\texample.com/pinned v1.2.0 => ../outside\n"
        "\texample.com/remote => example.com/fork v1.0.0\n"
        '\t"example.com/quoted" v0.1.0 => ./sub\n'
        ")\n",
        encoding="utf-8",
    )
    assert go.local_replacements(tmp_path / "go.mod", tmp_path) == [
        ("example.com/new", "sub"),
        ("example.com/quoted", "sub"),
    ]


def test_replace_aliases_meet_the_nested_definition():
    old, new = "example.com/old", "example.com/new"
    root = Index()
    _document(root, "main.go", [(1, _symbol(new, f"{new}/pkg", "Foo()."), 0)])
    sub = Index()
    _document(sub, "pkg/foo.go", [(2, _symbol(old, f"{old}/pkg", "Foo()."), 1)])
    merged = go.merge_module_indexes(
        root, [(old, "sub", sub)], ROOT, aliases=[(new, "sub")]
    )
    symbols = {d.occurrences[0].symbol for d in merged.documents}
    assert symbols == {_symbol(ROOT, f"{ROOT}/sub/pkg", "Foo().")}
