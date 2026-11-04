# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Replay captured pyrefly Glean reports through the scip-python-shaped converter.

Each capture is ``pyrefly check --report-glean`` (pyrefly 1.4.0) output for an
identity fixture source from ``scip_python_identity``, reduced to the
predicates the converter reads. The expectations mirror
``test_python_identity.py``, which reaches them only after
``normalize_python_index`` repairs scip-python output.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.protobuf.text_format import MessageToString

from codenib.scip_interface.lsp_occurrence_index import SCIPOccurrenceIndex
from codenib.scip_interface.pyrefly_glean import (
    _Converter,
    _Definition,
    _python_parser,
    _receiver_writes,
    glean_to_scip_index,
)
from codenib.scip_interface.pyrefly_provider import write_pyrefly_config
from codenib.scip_interface.python_identity import normalize_python_index
from codenib.scip_interface.scip_decode_python import SCIPPythonGraphDecoder
from codenib.types import EDGE_TYPE_CONTAIN, EDGE_TYPE_REFERENCE

FIXTURES = Path(__file__).parents[1] / "fixtures"
CAPTURES = FIXTURES / "pyrefly_glean"
IDENTITY = FIXTURES / "scip_python_identity"
NESTED_COLLISION = """def left():
    return 1

def right():
    return 2

class Example:
    def run(self, flag):
        if flag:
            def run():
                return left()
        else:
            def run():
                return right()
        return run()
"""


def _sources(root: Path, case: str) -> None:
    if case == "nested_collision":
        (root / "nested.py").write_text(NESTED_COLLISION)
        return
    if case.startswith("receivers_"):
        rows = json.loads((IDENTITY / "attribute_write_receivers.fixture").read_text())
        [row] = [r for r in rows if r["case"] == case.removeprefix("receivers_")]
        (root / "nested.py").write_text(row["source"])
        return
    (root / "nested.py").write_bytes((IDENTITY / f"{case}.source").read_bytes())
    other = IDENTITY / f"{case}.other.source"
    if other.exists():
        (root / "other.py").write_bytes(other.read_bytes())


def _convert(tmp_path, case):
    _sources(tmp_path, case)
    index, stats = glean_to_scip_index(
        CAPTURES / case, tmp_path, project_name="fixture", project_version="1"
    )
    text = MessageToString(index, as_utf8=True)
    decoded = tmp_path / "index.decoded"
    decoded.write_text(text)
    graph = SCIPPythonGraphDecoder(str(decoded), str(tmp_path)).decode()
    return index, graph, SCIPOccurrenceIndex.from_decoded_text(text)


def _lines(positions, line, character, file="nested.py"):
    try:
        rows = positions.definitions(file_path=file, line=line, character=character)
    except ValueError:
        return set()
    return {row.start_line for row in rows}


def _references(graph, attribute="unified_name"):
    vs = graph.graph.vs
    return {
        (vs[e.source][attribute], vs[e.target][attribute])
        for e in graph.graph.es
        if e["type"] == EDGE_TYPE_REFERENCE
    }


def test_nested_same_named_functions_keep_their_callers(tmp_path):
    # #871: scip-python flattens both inner `run`s into the method.
    index, graph, positions = _convert(tmp_path, "nested_collision")
    rows = index.documents[0].occurrences
    definitions = [
        row
        for row in rows
        if row.symbol_roles & 1
        and row.range[0] in {7, 9, 12}
        and row.range[1] in {8, 16}
    ]
    assert [row.range[0] for row in definitions] == [7, 9, 12]
    assert len({row.symbol for row in definitions}) == 3
    assert list(definitions[2].enclosing_range) == [12, 12, 13, 30]
    runs = {
        v["selection_line"]: v
        for v in graph.graph.vs
        if (v.attributes().get("unified_name") or "").endswith("run()")
    }
    assert set(runs) == {7, 9, 12}
    assert _references(graph, "name") == {
        (runs[9]["name"], "nested.py:left()"),
        (runs[12]["name"], "nested.py:right()"),
        (runs[7]["name"], runs[9]["name"]),
        (runs[7]["name"], runs[12]["name"]),
    }
    assert _lines(positions, 14, 15) == {9, 12}


def test_noncyclic_collisions_keep_each_call_in_its_lexical_function(tmp_path):
    _, graph, _ = _convert(tmp_path, "noncyclic")
    assert _references(graph) == {
        ("nested.py:Graph.order().visit()", "nested.py:left()"),
        ("nested.py:Graph.order()", "nested.py:Graph.order().visit()"),
        ("nested.py:Graph.check().visit()", "nested.py:right()"),
        ("nested.py:Graph.check()", "nested.py:Graph.check().visit()"),
        ("nested.py:external()", "nested.py:Graph"),
        ("nested.py:external()", "nested.py:Graph.visit()"),
    }


def test_branch_parameters_keep_their_physical_function(tmp_path):
    index, _, _ = _convert(tmp_path, "parameters")
    rows = index.documents[0].occurrences
    parameters = {
        row.range[0]: row.symbol
        for row in rows
        if row.symbol_roles & 1 and row.symbol.endswith("(v)")
    }
    assert set(parameters) == {3, 7} and parameters[3] != parameters[7]
    for row in rows:
        if row.symbol_roles & 8 and row.symbol.endswith("(v)"):
            assert row.symbol == parameters[3 if row.range[0] in {4, 5} else 7]


@pytest.mark.parametrize(
    ("case", "uses", "expected"),
    [
        ("rebinding", [(3, 12), (6, 13), (8, 26)], {1, 4, 7}),
        ("properties", [(2, 13), (10, 20)], {4, 7}),
        ("field_redefinitions", [(6, 17)], {2, 5}),
        ("field_redefinitions", [(11, 22)], {10}),
        ("field_redefinitions", [(15, 22)], {14}),
        ("varargs", [(3, 19), (3, 28)], {2}),
        ("varargs", [(7, 19), (7, 28)], {6}),
        ("attribute_fields", [(4, 20)], {3}),
        ("attribute_fields", [(9, 20)], {8}),
        ("attribute_writes", [(5, 17), (8, 17)], {3}),
        ("attribute_writes", [(15, 17), (18, 17)], {13}),
        ("decorated_field_writes", [(18, 21), (22, 23)], {14}),
    ],
)
def test_uses_resolve_to_their_physical_declarations(tmp_path, case, uses, expected):
    index, _, positions = _convert(tmp_path, case)
    for line, character in uses:
        assert _lines(positions, line, character) == expected
    if case in {"attribute_writes", "decorated_field_writes"}:
        writes = {line for line, _ in uses[:1]}
        rows = [r for r in index.documents[0].occurrences if r.range[0] in writes]
        assert not any(row.symbol_roles & 1 for row in rows)


def test_repeated_classes_keep_unique_containment_parents(tmp_path):
    _, graph, _ = _convert(tmp_path, "field_redefinitions")
    parents = {}
    for edge in graph.graph.es:
        if edge["type"] == EDGE_TYPE_CONTAIN:
            parents.setdefault(edge.target, set()).add(edge.source)
    assert all(len(values) == 1 for values in parents.values())


def test_utf8_columns_and_inner_unicode_functions(tmp_path):
    index, _, positions = _convert(tmp_path, "unicode")
    line = (tmp_path / "nested.py").read_text().splitlines()[10]
    column = len(line.split("运行", 1)[0].encode("utf-8"))
    assert _lines(positions, 10, column) == {5, 8}
    assert index.documents[0].position_encoding == 1


def test_pep695_type_bindings_do_not_make_their_owner_contain_itself(tmp_path):
    _, graph, _ = _convert(tmp_path, "type_parameters")
    graph.validate_containment()
    names = {vertex["unified_name"] for vertex in graph.graph.vs}
    assert {
        "nested.py:Box",
        "nested.py:Box.__init__()",
        "nested.py:Box.get()",
        "nested.py:first()",
        "nested.py:build()",
        "nested.py:build().Edge",
        "nested.py:build().Edge.node",
    } <= names
    assert not any(edge.is_loop() for edge in graph.graph.es)


def test_local_bindings_are_document_scoped(tmp_path):
    _, _, positions = _convert(tmp_path, "local_bindings")
    for file in ["nested.py", "other.py"]:
        definitions = positions.definitions(file_path=file, line=4, character=12)
        assert {(row.file_path, row.start_line) for row in definitions} == {
            (file, 1),
            (file, 2),
        }
        assert _lines(positions, 10, 8, file) == {10}


@pytest.mark.parametrize(
    "case",
    [
        "receivers_other_parameter",
        "receivers_staticmethod",
        "receivers_assignment_rebinding",
        "receivers_loop_rebinding",
        "receivers_pattern_rebinding",
    ],
)
def test_field_reads_keep_their_function_local_class(tmp_path, case):
    _, _, positions = _convert(tmp_path, case)
    lines = (tmp_path / "nested.py").read_text().splitlines()
    for function in ("one", "two"):
        start = lines.index(f"def {function}():")
        init = next(i for i in range(start, len(lines)) if "closed = False" in lines[i])
        read = next(
            i for i in range(init, len(lines)) if "return owner.closed" in lines[i]
        )
        assert _lines(positions, read, lines[read].index(".closed") + 1) == {init}


@pytest.mark.parametrize(
    "case", ["receivers_other_parameter", "receivers_staticmethod"]
)
def test_known_gap_writes_through_another_receiver_have_no_reference(tmp_path, case):
    # pyrefly 1.4 reports no cross reference at an attribute write. The
    # converter recovers writes through a method's own receiver only.
    _, _, positions = _convert(tmp_path, case)
    lines = (tmp_path / "nested.py").read_text().splitlines()
    write = next(i for i, text in enumerate(lines) if ".closed = True" in text)
    assert _lines(positions, write, lines[write].index(".closed") + 1) == set()


def test_known_gap_module_comprehension_walrus_has_no_definition(tmp_path):
    # pyrefly 1.4 reports no definition for `text` bound by a walrus inside
    # a module-level comprehension; scip-python reports a bare `text.`.
    index, _, _ = _convert(tmp_path, "module_walrus")
    assert not [o for o in index.documents[0].occurrences if "text" in o.symbol]


@pytest.mark.parametrize(
    "case", sorted(path.name for path in CAPTURES.iterdir() if path.is_dir())
)
def test_identity_recovery_has_nothing_to_repair(tmp_path, case):
    index, _, _ = _convert(tmp_path, case)

    def canonical(value):
        rows = []
        for document in value.documents:
            for row in document.occurrences:
                span = list(row.range)
                if len(span) == 3:
                    span = [span[0], span[1], span[0], span[2]]
                rows.append((document.relative_path, tuple(span), row.symbol))
        return sorted(rows)

    before = canonical(index)
    report = normalize_python_index(
        index,
        tmp_path,
        resolve_definitions=lambda *_: pytest.fail("no positional query is needed"),
    )
    assert report["changed_symbol_families"] == 0
    assert report["source_only_definitions"] == []
    assert canonical(index) == before


@pytest.mark.parametrize(
    "case", sorted(path.name for path in CAPTURES.iterdir() if path.is_dir())
)
def test_pyrefly_capture_preserves_serial_native_query_surface(tmp_path, case):
    pytest.importorskip("codenib_core")
    from codenib.scip_interface.query_surface import query_surface_sha256
    from codenib.scip_interface.scip_decode_core import SCIPDecoderCore

    _, serial, _ = _convert(tmp_path, case)
    native = SCIPDecoderCore(
        str(tmp_path / "index.decoded"), str(tmp_path), language="python"
    ).decode()
    assert query_surface_sha256(native) == query_surface_sha256(serial)


def test_config_checks_every_source_with_src_layout_and_pyright_scope(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[tool.pyright]\ninclude = ["src", "tests"]\nexclude = ["tests/data"]\n'
    )
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        import tomli as tomllib

    config = tomllib.loads(
        write_pyrefly_config(
            tmp_path / "pyrefly.toml", root, exclude_patterns=["**/build/**"]
        ).read_text()
    )
    base = root.resolve().as_posix()
    assert config["search-path"] == [f"{base}/src", base]
    assert f"{base}/src/**/*.py" in config["project-includes"]
    assert f"{base}/tests/**/*.py" in config["project-includes"]
    assert f"{base}/tests/data/**" in config["project-excludes"]
    assert f"{base}/**/build/**" in config["project-excludes"]
    assert config["disable-project-excludes-heuristics"] is True
    assert not any(path.name == "pyrefly.toml" for path in root.rglob("*"))


@pytest.mark.parametrize("include", ["**/*.py", "src/*/api?.py", "pkg/**/*.pyi"])
def test_pyright_include_globs_remain_globs(tmp_path, include):
    (tmp_path / "pyrightconfig.json").write_text(json.dumps({"include": [include]}))
    config = write_pyrefly_config(tmp_path / "generated.toml", tmp_path).read_text()
    assert f'"{tmp_path.as_posix()}/{include}"' in config
    assert f"{include}/**/*.py" not in config


@pytest.mark.parametrize(
    "binding",
    [
        "self = Other()",
        "self, other = others",
        "self += other",
        "if (self := other): pass",
        "for self in others: pass",
        "with other as self: pass",
        "try: pass\nexcept Exception as self: pass",
        "match other:\n    case self: pass",
        "del self",
        "import other as self",
        "from other import self",
        "def self(): pass",
        "class self: pass",
    ],
)
def test_rebound_receiver_never_synthesizes_owner_fields(binding):
    body = "\n".join("    " + line for line in binding.splitlines())
    # A write before rebinding must not escape the whole-body guard either.
    source = (
        f"def close(self):\n    self.before = True\n{body}\n    self.closed = True\n"
    )
    tree = _python_parser().parse(source.encode())
    assert not tree.root_node.has_error
    method = tree.root_node.named_children[0]
    assert list(_receiver_writes(method.child_by_field_name("body"), b"self")) == []


def test_receiver_guard_ignores_nested_scope_and_attribute_assignments():
    tree = _python_parser().parse(
        b"def close(self):\n"
        b"    def inner(self):\n        self = Other()\n        self.hidden = True\n"
        b"    self.closed = True\n    self.items[0] = other\n"
    )
    method = tree.root_node.named_children[0]
    writes = list(_receiver_writes(method.child_by_field_name("body"), b"self"))
    assert [name for _, _, name in writes] == ["closed"]


def _hierarchy_converter(bases, members, aliases=None):
    converter = object.__new__(_Converter)
    converter.report = SimpleNamespace(aliases=aliases or {})
    converter.registry = {
        name: [_Definition("cls", name, "mod.py", 0, 1, None, None, bases=parents)]
        for name, parents in bases.items()
    }
    for name in members:
        converter.registry[name] = [
            _Definition("variable", name, "mod.py", 0, 1, None, None, symbol=name)
        ]
    return converter


def test_inherited_member_matches_python_c3_instead_of_breadth_first():
    class Root:
        value = "Root.value"

    class Left(Root):
        pass

    class Right:
        value = "Right.value"

    class Child(Left, Right):
        pass

    converter = _hierarchy_converter(
        {"Child": ("Left", "Alias"), "Left": ("Root",), "Right": (), "Root": ()},
        ["Root.value", "Right.value"],
        {"Alias": "Right"},
    )
    assert converter._inherited("Child", "value", 0) == [Child.value]
    assert [cls.__name__ for cls in Child.__mro__][1:4] == ["Left", "Root", "Right"]


@pytest.mark.parametrize(
    "bases,aliases",
    [
        ({"Child": ("Child",)}, {}),
        ({"Child": ("Missing", "Root"), "Root": ()}, {}),
        ({"Child": ("Alias",)}, {"Alias": "Other", "Other": "Alias"}),
        (
            {
                "Child": ("Left", "Right"),
                "Left": ("A", "B"),
                "Right": ("B", "A"),
                "A": (),
                "B": (),
            },
            {},
        ),
    ],
)
def test_unproven_mro_does_not_choose_an_inherited_member(bases, aliases):
    converter = _hierarchy_converter(bases, ["Root.value", "A.value"], aliases)
    assert converter._inherited("Child", "value", 0) == []
