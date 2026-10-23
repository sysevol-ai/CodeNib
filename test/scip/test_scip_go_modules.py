# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Go graphs cover nested modules and test files."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from google.protobuf.text_format import MessageToString

from codenib.scip_interface import go_modules as gm
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
    assert gm.nested_go_modules(tmp_path) == [
        (SERVER, "server"),
        ("example.test/repo/tools/gen", "tools/gen"),
    ]
    assert gm.go_module_path(tmp_path / "go.mod") == ROOT


def test_rename_moves_nested_packages_under_the_root_module():
    directories = {SERVER: ("server", None)}
    rename = gm.rename_module_symbol
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
    merged = gm.merge_module_indexes(
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
    merged = gm.merge_module_indexes(
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

    merged = gm.merge_module_indexes(
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
    merged = gm.merge_module_indexes(_root_index(), [(SERVER, "server", server)], ROOT)
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
    merged = gm.merge_module_indexes(
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
        f"\texample.com/absolute => {tmp_path / 'sub'}\n"
        "\texample.com/elsewhere => /opt/go/mod\n"
        ")\n",
        encoding="utf-8",
    )
    assert gm.local_replacements(tmp_path / "go.mod", tmp_path) == [
        gm.Replacement("example.com/new", None, "sub"),
        gm.Replacement("example.com/quoted", "v0.1.0", "sub"),
        gm.Replacement("example.com/absolute", None, "sub"),
    ]


def test_replace_aliases_meet_the_nested_definition():
    old, new = "example.com/old", "example.com/new"
    root = Index()
    _document(root, "main.go", [(1, _symbol(new, f"{new}/pkg", "Foo()."), 0)])
    sub = Index()
    _document(sub, "pkg/foo.go", [(2, _symbol(old, f"{old}/pkg", "Foo()."), 1)])
    merged = gm.merge_module_indexes(
        root,
        [(old, "sub", sub)],
        ROOT,
        replacements={"": [gm.Replacement(new, None, "sub")]},
    )
    symbols = {d.occurrences[0].symbol for d in merged.documents}
    assert symbols == {_symbol(ROOT, f"{ROOT}/sub/pkg", "Foo().")}


def test_replacements_apply_only_to_the_declaring_module():
    dep, local = "example.com/dep", "example.test/local"
    root = Index()
    _document(root, "main.go", [(1, _symbol(dep, f"{dep}/x", "Run().", "v1.0.0"), 0)])
    a = Index()
    _document(a, "a.go", [(1, _symbol(dep, f"{dep}/x", "Run().", "v0"), 0)])
    merged = gm.merge_module_indexes(
        root,
        [("example.test/a", "a", a)],
        ROOT,
        modules=[(local, "local")],
        replacements={"a": [gm.Replacement(dep, None, "local")]},
    )
    assert [d.occurrences[0].symbol for d in merged.documents] == [
        _symbol(dep, f"{dep}/x", "Run().", "v1.0.0"),
        _symbol(ROOT, f"{ROOT}/local/x", "Run()."),
    ]


def test_covered_modules_are_the_deepest_directories(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    for directory, module in [(".", ROOT), ("server", SERVER), ("server/tools", "t")]:
        (repository / directory).mkdir(parents=True, exist_ok=True)
        (repository / directory / "go.mod").write_text(f"module {module}\n")
    indexer = go.SCIPGoIndexer(repository, output_dir=tmp_path / "state")
    monkeypatch.setattr(indexer, "_check_indexer_available", lambda: True)
    calls = []

    def run(command, **kwargs):
        cwd = Path(kwargs["cwd"])
        calls.append(cwd.relative_to(repository).as_posix())
        index = _root_index()
        if cwd == repository:
            # The workspace pulled in only server/tools.
            index.documents.add(relative_path="server/tools/gen.go")
        Path(command[command.index("--output") + 1]).write_bytes(
            index.SerializeToString()
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert indexer.generate_index() is True
    assert calls == [".", "server"]


def test_go_files_follow_the_module_grammar(tmp_path):
    (tmp_path / "local module").mkdir()
    (tmp_path / "go.mod").write_text(
        "module (\n"
        "\texample.test/blocky // a comment\n"
        ")\n"
        'replace example.com/spaced => "./local module"\n'
        "replace example.com/pinned v1.0.0 => `./local module`\n"
        "replace example.com/module => example.com/fork v1.2.0\n",
        encoding="utf-8",
    )
    assert gm.go_module_path(tmp_path / "go.mod") == "example.test/blocky"
    assert gm.local_replacements(tmp_path / "go.mod", tmp_path) == [
        gm.Replacement("example.com/spaced", None, "local module"),
        gm.Replacement("example.com/pinned", "v1.0.0", "local module"),
    ]


def test_versioned_replacements_apply_only_to_their_version():
    mapping = {"example.com/dep": ("local", "v1.0.0")}
    pinned = _symbol("example.com/dep", "example.com/dep/x", "Run().", "v1.0.0")
    other = _symbol("example.com/dep", "example.com/dep/x", "Run().", "v1.1.0")
    assert gm.rename_module_symbol(pinned, ROOT, mapping) == _symbol(
        ROOT, f"{ROOT}/local/x", "Run().", "v1.0.0"
    )
    assert gm.rename_module_symbol(other, ROOT, mapping) == other


def test_workspace_and_covered_module_replacements_apply_in_the_root_run():
    dep, new = "example.com/dep", "example.com/new"
    root = Index()
    # The root module imports a workspace alias; the covered server module
    # uses its own replacement.
    _document(root, "main.go", [(1, _symbol(new, f"{new}/pkg", "Foo()."), 0)])
    _document(root, "server/s.go", [(1, _symbol(dep, f"{dep}/x", "Run()."), 0)])
    merged = gm.merge_module_indexes(
        root,
        [],
        ROOT,
        modules=[(SERVER, "server"), ("example.test/sub", "sub")],
        replacements={"server": [gm.Replacement(dep, None, "local")]},
        workspace=[gm.Replacement(new, None, "sub")],
    )
    assert [d.occurrences[0].symbol for d in merged.documents] == [
        _symbol(ROOT, f"{ROOT}/sub/pkg", "Foo()."),
        _symbol(ROOT, f"{ROOT}/local/x", "Run()."),
    ]


def test_active_workspace_honors_gowork(tmp_path, monkeypatch):
    (tmp_path / "go.work").write_text("go 1.22\nuse ./server\n", encoding="utf-8")
    monkeypatch.delenv("GOWORK", raising=False)
    assert gm.active_workspace(tmp_path) == tmp_path / "go.work"
    monkeypatch.setenv("GOWORK", "off")
    assert gm.active_workspace(tmp_path) is None
