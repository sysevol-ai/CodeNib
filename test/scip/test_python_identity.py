# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Reproduce #871 using captured provider output and exact source positions."""

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest
from google.protobuf.text_format import MessageToString, Parse

from codenib.scip_interface import python_identity
from codenib.scip_interface.lsp_occurrence_index import SCIPOccurrenceIndex
from codenib.scip_interface.python_identity import normalize_python_index
from codenib.scip_interface.scip_decode_python import SCIPPythonGraphDecoder
from codenib.scip_interface.scip_pb2 import Index
from codenib.types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
)

SOURCE = """def left():
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

CAPTURES = Path(__file__).parents[1] / "fixtures/scip_python_identity"


def _captured_fixture(root, name):
    (root / "nested.py").write_bytes((CAPTURES / f"{name}.source").read_bytes())
    other = CAPTURES / f"{name}.other.source"
    if other.exists():
        (root / "other.py").write_bytes(other.read_bytes())
    index = Parse((CAPTURES / f"{name}.decoded").read_text(), Index())
    capture = json.loads((CAPTURES / f"{name}.definitions.json").read_text())
    replies = {tuple(row["request"]): row["response"] for row in capture["queries"]}
    queries = []

    def resolve(file, line, character):
        key = (file, line, character)
        queries.append(key)
        locations = copy.deepcopy(replies[key])
        for location in locations:
            for field in ("uri", "targetUri"):
                relative = location.pop(field + "_relative", None)
                if relative is not None:
                    location[field] = (root / relative).as_uri()
        return locations

    return index, resolve, queries


def _fixture(tmp_path):
    (tmp_path / "nested.py").write_text(SOURCE)
    fixture = (
        Path(__file__).parents[1] / "fixtures/scip_python_nested_collision.fixture"
    )
    return Parse(fixture.read_text(), Index())


def _resolver(root, observed):
    def resolve(file, line, character):
        observed.append((file, line, character))
        assert (file, line, character) == ("nested.py", 14, 15)
        return [
            {
                "uri": (root / file).as_uri(),
                "range": {
                    "start": {"line": target, "character": 16},
                    "end": {"line": target, "character": 19},
                },
            }
            for target in (9, 12)
        ]

    return resolve


def _outside_family(root):
    # `left` is a declaration, but not one of the `run` family.
    def resolve(file, *_):
        start = {"line": 0, "character": 4}
        return [{"uri": (root / file).as_uri(), "range": {"start": start}}]

    return resolve


def test_captured_definitions_and_branch_targets_are_recovered(tmp_path):
    index = _fixture(tmp_path)
    observed = []
    report = normalize_python_index(
        index, tmp_path, resolve_definitions=_resolver(tmp_path, observed)
    )

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
    targets = [row.symbol for row in rows if row.range[0] == 14]
    assert set(targets) == {definitions[1].symbol, definitions[2].symbol}
    assert definitions[0].symbol not in targets
    assert report["restored_definition_roles"] == 1
    assert observed == [("nested.py", 14, 15)]


def test_recovery_is_idempotent_without_another_lsp_query(tmp_path):
    index = _fixture(tmp_path)
    normalize_python_index(index, tmp_path, resolve_definitions=_resolver(tmp_path, []))
    first = index.SerializeToString()
    report = normalize_python_index(
        index,
        tmp_path,
        resolve_definitions=lambda *_: pytest.fail(
            "normalized identities need no query"
        ),
    )

    assert index.SerializeToString() == first
    assert report["changed_symbol_families"] == 0


@pytest.mark.parametrize(
    "case", sorted({path.name.split(".")[0] for path in CAPTURES.glob("*.decoded")})
)
def test_recovering_a_recovered_index_changes_nothing(tmp_path, case):
    # Cached builds rerun recovery on the already-normalized index.decoded.
    index, resolve, _ = _captured_fixture(tmp_path, case)
    normalize_python_index(index, tmp_path, resolve_definitions=resolve)
    first = index.SerializeToString()

    report = normalize_python_index(
        index,
        tmp_path,
        resolve_definitions=lambda *_: pytest.fail(
            "normalized identities need no query"
        ),
    )

    assert index.SerializeToString() == first
    assert report["changed_symbol_families"] == 0


def test_unresolved_target_never_publishes_a_partial_repair(tmp_path):
    index = _fixture(tmp_path)
    original = index.SerializeToString()

    with pytest.raises(ValueError, match="target is unresolved"):
        normalize_python_index(
            index, tmp_path, resolve_definitions=_outside_family(tmp_path)
        )

    assert index.SerializeToString() == original


def test_reference_without_a_provider_definition_keeps_the_static_family(
    tmp_path,
):
    index = _fixture(tmp_path)
    report = normalize_python_index(index, tmp_path, resolve_definitions=lambda *_: [])

    rows = index.documents[0].occurrences
    definitions = {
        row.symbol
        for row in rows
        if row.symbol_roles & 1 and row.range[:2] in ([7, 8], [9, 16], [12, 16])
    }
    assert len(definitions) == 3
    assert {row.symbol for row in rows if row.range[0] == 14} == definitions
    [query] = report["unanswered_definition_queries"]
    assert (query["file"], query["line"], query["character"]) == ("nested.py", 14, 15)


def test_provider_timeout_never_publishes_a_repair(tmp_path, monkeypatch):
    from codenib import toolchains
    from codenib.graph.incremental import lsp_client

    index = _fixture(tmp_path)
    original = index.SerializeToString()
    shutdowns = []

    class TimingOutClient:
        _last_error = None

        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def get_lsp_command(_):
            return ["basedpyright-langserver", "--stdio"]

        def start(self, **_kwargs):
            return {"capabilities": {"positionEncoding": "utf-16"}}

        def definition(self, *_args, **_kwargs):
            self._last_error = {"code": -1, "message": "timeout"}
            return []

        def shutdown(self):
            shutdowns.append(self)

    monkeypatch.setattr(lsp_client, "LSPClient", TimingOutClient)
    monkeypatch.setattr(toolchains, "resolve_command", lambda _: "/tools/basedpyright")
    with pytest.raises(RuntimeError, match="definition query failed .*timeout"):
        normalize_python_index(index, tmp_path)

    assert len(shutdowns) == 1
    assert index.SerializeToString() == original


def test_source_edit_during_lookup_never_publishes_a_repair(tmp_path):
    index = _fixture(tmp_path)
    original = index.SerializeToString()
    resolve = _resolver(tmp_path, [])

    def edit(*args):
        (tmp_path / "nested.py").write_text(SOURCE.replace("return 1", "return 3"))
        return resolve(*args)

    with pytest.raises(RuntimeError, match="source changed during identity recovery"):
        normalize_python_index(index, tmp_path, resolve_definitions=edit)

    assert index.SerializeToString() == original


def test_recovered_graph_preserves_callers_and_both_branch_definitions(tmp_path):
    index = _fixture(tmp_path)
    normalize_python_index(index, tmp_path, resolve_definitions=_resolver(tmp_path, []))
    decoded = tmp_path / "index.decoded"
    decoded.write_text(MessageToString(index, as_utf8=True))
    graph = SCIPPythonGraphDecoder(str(decoded), str(tmp_path)).decode()
    graph.save_graph(str(tmp_path / "graph.pkl"))

    runs = {
        v["selection_line"]: v
        for v in graph.graph.vs
        if (v.attributes().get("unified_name") or "").endswith("run()")
    }
    assert set(runs) == {7, 9, 12}
    assert runs[7]["type"] == NODE_TYPE_METHOD
    assert runs[9]["type"] == runs[12]["type"] == NODE_TYPE_FUNCTION
    assert runs[9]["unified_name"] == runs[12]["unified_name"]
    edges = {
        (graph.graph.vs[e.source]["name"], graph.graph.vs[e.target]["name"])
        for e in graph.graph.es
        if e["type"] == EDGE_TYPE_REFERENCE
    }
    assert edges == {
        (runs[9]["name"], "nested.py:left()"),
        (runs[12]["name"], "nested.py:right()"),
        (runs[7]["name"], runs[9]["name"]),
        (runs[7]["name"], runs[12]["name"]),
    }
    positions = SCIPOccurrenceIndex.from_decoded_file(decoded)
    definitions = positions.definitions(file_path="nested.py", line=14, character=15)
    assert {d.start_line for d in definitions} == {9, 12}
    assert all(d.file_path == "nested.py" for d in definitions)
    assert (
        positions.references(
            file_path="nested.py", line=7, character=8, include_declaration=False
        )
        == []
    )


def test_reassigned_and_nonlocal_parameters_keep_their_own_physical_binding(tmp_path):
    index, resolve, queries = _captured_fixture(tmp_path, "parameters")
    normalize_python_index(index, tmp_path, resolve_definitions=resolve)
    rows = index.documents[0].occurrences
    parameters = {
        row.range[0]: row.symbol
        for row in rows
        if row.symbol_roles & 1 and row.symbol.endswith("(v)")
    }

    assert set(parameters) == {3, 7}
    assert parameters[3] != parameters[7]
    for row in rows:
        if row.symbol_roles & 8 and row.symbol.endswith("(v)"):
            owner = 3 if row.range[0] in {4, 5} else 7
            assert row.symbol == parameters[owner]
    assert ("nested.py", 11, 20) in queries


def test_rebinding_keeps_both_classes_and_the_assignment_as_possible_targets(tmp_path):
    index, resolve, _ = _captured_fixture(tmp_path, "rebinding")
    report = normalize_python_index(index, tmp_path, resolve_definitions=resolve)
    decoded = MessageToString(index, as_utf8=True)
    positions = SCIPOccurrenceIndex.from_decoded_text(decoded)

    for line, character in [(3, 12), (6, 13), (8, 26)]:
        definitions = positions.definitions(
            file_path="nested.py", line=line, character=character
        )
        assert {row.start_line for row in definitions} == {1, 4, 7}
    assert report["restored_definition_roles"] == 2
    first = index.SerializeToString()
    normalize_python_index(
        index,
        tmp_path,
        resolve_definitions=lambda *_: pytest.fail("recovery must be idempotent"),
    )
    assert index.SerializeToString() == first


def test_noncyclic_collisions_keep_each_call_in_its_lexical_function(tmp_path):
    index, resolve, _ = _captured_fixture(tmp_path, "noncyclic")
    normalize_python_index(index, tmp_path, resolve_definitions=resolve)
    decoded = tmp_path / "index.decoded"
    decoded.write_text(MessageToString(index, as_utf8=True))
    graph = SCIPPythonGraphDecoder(str(decoded), str(tmp_path)).decode()
    graph.save_graph(str(tmp_path / "graph.pkl"))
    edges = {
        (
            graph.graph.vs[e.source]["unified_name"],
            graph.graph.vs[e.target]["unified_name"],
        )
        for e in graph.graph.es
        if e["type"] == EDGE_TYPE_REFERENCE
    }

    assert edges == {
        ("nested.py:Graph.order().visit()", "nested.py:left()"),
        ("nested.py:Graph.order()", "nested.py:Graph.order().visit()"),
        ("nested.py:Graph.check().visit()", "nested.py:right()"),
        ("nested.py:Graph.check()", "nested.py:Graph.check().visit()"),
        ("nested.py:external()", "nested.py:Graph"),
        ("nested.py:external()", "nested.py:Graph.visit()"),
    }


def test_utf16_provider_columns_become_exact_utf8_query_positions(tmp_path):
    index, resolve, queries = _captured_fixture(tmp_path, "unicode")
    normalize_python_index(index, tmp_path, resolve_definitions=resolve)
    line = (tmp_path / "nested.py").read_text().splitlines()[10]
    prefix = line.split("运行", 1)[0]
    utf8_column = len(prefix.encode("utf-8"))
    utf16_column = len(prefix.encode("utf-16-le")) // 2
    assert utf8_column != utf16_column
    assert ("nested.py", 10, utf16_column) in queries
    positions = SCIPOccurrenceIndex.from_decoded_text(
        MessageToString(index, as_utf8=True)
    )
    definitions = positions.definitions(
        file_path="nested.py", line=10, character=utf8_column
    )
    assert {row.start_line for row in definitions} == {5, 8}
    assert index.documents[0].position_encoding == 1


def test_local_ids_are_scoped_to_the_document_and_keep_all_binding_declarations(
    tmp_path,
):
    index, resolve, queries = _captured_fixture(tmp_path, "local_bindings")
    report = normalize_python_index(index, tmp_path, resolve_definitions=resolve)
    positions = SCIPOccurrenceIndex.from_decoded_text(
        MessageToString(index, as_utf8=True)
    )

    for file in ["nested.py", "other.py"]:
        definitions = positions.definitions(file_path=file, line=4, character=12)
        assert {(row.file_path, row.start_line) for row in definitions} == {
            (file, 1),
            (file, 2),
        }
        source_only = positions.definitions(file_path=file, line=10, character=8)
        assert {(row.file_path, row.start_line) for row in source_only} == {(file, 10)}
    assert report["local_binding_families"] == 4
    assert {row["file"] for row in report["source_only_definitions"]} == {
        "nested.py",
        "other.py",
    }
    assert queries == []
    first = index.SerializeToString()
    normalize_python_index(index, tmp_path, resolve_definitions=resolve)
    assert index.SerializeToString() == first


def test_property_reads_and_writes_keep_the_static_accessor_binding(tmp_path):
    index, resolve, _ = _captured_fixture(tmp_path, "properties")
    normalize_python_index(index, tmp_path, resolve_definitions=resolve)
    positions = SCIPOccurrenceIndex.from_decoded_text(
        MessageToString(index, as_utf8=True)
    )

    for line in [2, 10]:
        definitions = positions.definitions(
            file_path="nested.py", line=line, character=20 if line == 10 else 13
        )
        assert {row.start_line for row in definitions} == {4, 7}


def test_repeated_class_members_keep_physical_definitions_and_unique_parents(tmp_path):
    index, resolve, _ = _captured_fixture(tmp_path, "field_redefinitions")
    normalize_python_index(index, tmp_path, resolve_definitions=resolve)
    decoded = tmp_path / "index.decoded"
    decoded.write_text(MessageToString(index, as_utf8=True))
    positions = SCIPOccurrenceIndex.from_decoded_file(decoded)
    definitions = positions.definitions(file_path="nested.py", line=6, character=17)
    assert {row.start_line for row in definitions} == {2, 5}
    for use, declaration in [(11, 10), (15, 14)]:
        definitions = positions.definitions(
            file_path="nested.py", line=use, character=22
        )
        assert {row.start_line for row in definitions} == {declaration}
    graph = SCIPPythonGraphDecoder(str(decoded), str(tmp_path)).decode()
    graph.save_graph(str(tmp_path / "graph.pkl"))
    parents = {}
    for edge in graph.graph.es:
        if edge["type"] == EDGE_TYPE_CONTAIN:
            parents.setdefault(edge.target, set()).add(edge.source)
    assert all(len(values) == 1 for values in parents.values())
    first = index.SerializeToString()
    normalize_python_index(index, tmp_path, resolve_definitions=resolve)
    assert index.SerializeToString() == first


def test_starred_lsp_parameter_selections_keep_the_physical_scope(tmp_path):
    index, resolve, _ = _captured_fixture(tmp_path, "varargs")
    normalize_python_index(index, tmp_path, resolve_definitions=resolve)
    positions = SCIPOccurrenceIndex.from_decoded_text(
        MessageToString(index, as_utf8=True)
    )

    for use, definition in [(3, 2), (7, 6)]:
        args = positions.definitions(file_path="nested.py", line=use, character=19)
        kwargs = positions.definitions(file_path="nested.py", line=use, character=28)
        assert {row.start_line for row in args} == {definition}
        assert {row.start_line for row in kwargs} == {definition}


def test_constructor_attributes_keep_their_owning_physical_class(tmp_path):
    index, resolve, _ = _captured_fixture(tmp_path, "attribute_fields")
    report = normalize_python_index(index, tmp_path, resolve_definitions=resolve)
    positions = SCIPOccurrenceIndex.from_decoded_text(
        MessageToString(index, as_utf8=True)
    )

    for use, declaration in [(4, 3), (9, 8)]:
        definitions = positions.definitions(
            file_path="nested.py", line=use, character=20
        )
        assert {row.start_line for row in definitions} == {declaration}
    assert report["unmapped_attribute_definitions"] == []
    first = index.SerializeToString()
    normalize_python_index(index, tmp_path, resolve_definitions=resolve)
    assert index.SerializeToString() == first


def test_standard_library_binding_is_not_reassigned_to_a_project_annotation(tmp_path):
    source = "class A:\n    __qualname__: str\nclass B:\n    __qualname__: str\n"
    (tmp_path / "nested.py").write_text(source)
    index = Index()
    document = index.documents.add()
    document.relative_path = "nested.py"
    module = document.occurrences.add()
    module.range.extend([0, 0, 0])
    module.symbol = "scip-python python fixture 1 nested/__init__:"
    module.symbol_roles = 1
    symbol = "scip-python python python-stdlib 3.12 builtins/__qualname__#"
    for line in [1, 3]:
        row = document.occurrences.add()
        row.range.extend([line, 4, 16])
        row.symbol = symbol
        row.symbol_roles = 8

    normalize_python_index(
        index,
        tmp_path,
        resolve_definitions=lambda *_: pytest.fail("stdlib has no project binding"),
    )

    rows = [o for o in index.documents[0].occurrences if o.range[0] in {1, 3}]
    assert len(rows) == 2
    assert all(row.symbol == symbol and row.symbol_roles == 8 for row in rows)


def test_legacy_encoding_keeps_raw_bytes_and_reports_the_compiler_text_view(tmp_path):
    index = Index()
    document = index.documents.add()
    document.relative_path = "legacy.py"
    occurrence = document.occurrences.add()
    occurrence.range.extend([2, 0, 1])
    occurrence.symbol = "scip-python python example 1 legacy/u."
    occurrence.symbol_roles = 1
    source = b"# coding: iso-8859-5\n# \xb1\xb6\xff\xe0\nu='\xae'\n"
    (tmp_path / "legacy.py").write_bytes(source)

    report = normalize_python_index(index, tmp_path)

    assert (tmp_path / "legacy.py").read_bytes() == source
    assert report["utf8_replacement_sources"] == ["legacy.py"]


def test_failed_recovery_preserves_decoded_input_and_clears_writer_receipts(
    tmp_path, monkeypatch
):
    from codenib.scip_interface.scip_indexer_python import SCIPPythonIndexer

    index = _fixture(tmp_path)
    indexer = SCIPPythonIndexer(tmp_path, output_dir=tmp_path / "index")
    original = MessageToString(index, as_utf8=True)
    indexer.decoded_file.write_text(original)
    indexer.decoded_input_receipt = {"old": True}
    indexer.graph_output_receipt = {"old": True}
    indexer.index_generation_report = {"status": "complete"}
    normalize = normalize_python_index
    monkeypatch.setattr(
        python_identity,
        "normalize_python_index",
        lambda payload, root, **kwargs: normalize(
            payload, root, resolve_definitions=_outside_family(root), **kwargs
        ),
    )

    graph = indexer.process_index(str(tmp_path / "graph.pkl"))

    assert graph is None
    assert indexer.decoded_file.read_text() == original
    assert not (tmp_path / "graph.pkl").exists()
    assert indexer.decoded_input_receipt is None
    assert indexer.graph_output_receipt is None
    assert indexer.index_generation_report["identity_recovery"]["status"] == "failed"


def test_changed_decoded_input_is_not_overwritten_by_identity_recovery(
    tmp_path, monkeypatch
):
    from codenib.scip_interface.scip_indexer_python import SCIPPythonIndexer

    index = _fixture(tmp_path)
    indexer = SCIPPythonIndexer(tmp_path, output_dir=tmp_path / "index")
    indexer.decoded_file.write_text(MessageToString(index, as_utf8=True))
    replacement = "metadata { version: 1 }\n"
    normalize = normalize_python_index

    def replace_input(payload, root, **kwargs):
        result = normalize(
            payload, root, resolve_definitions=_resolver(tmp_path, []), **kwargs
        )
        indexer.decoded_file.write_text(replacement)
        return result

    monkeypatch.setattr(python_identity, "normalize_python_index", replace_input)

    graph = indexer.process_index(str(tmp_path / "graph.pkl"))

    assert graph is None
    assert indexer.decoded_file.read_text() == replacement
    assert not (tmp_path / "graph.pkl").exists()
    assert not list(indexer.decoded_file.parent.glob(".python-identity-*"))
    assert indexer.decoded_input_receipt is None
    assert indexer.graph_output_receipt is None


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_resolver_startup_failure_or_cancellation_always_shuts_down(
    tmp_path, monkeypatch, failure
):
    from codenib import toolchains
    from codenib.graph.incremental import lsp_client

    index = _fixture(tmp_path)
    original = index.SerializeToString()
    shutdowns = []

    class FailingClient:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def get_lsp_command(_):
            return ["basedpyright-langserver", "--stdio"]

        def start(self, *, skip_probe, initialize_timeout):
            assert skip_probe
            assert 0 < initialize_timeout <= 120
            raise failure("initialize failed")

        def shutdown(self):
            shutdowns.append(self)

    monkeypatch.setattr(lsp_client, "LSPClient", FailingClient)
    monkeypatch.setattr(toolchains, "resolve_command", lambda _: "/tools/basedpyright")
    with pytest.raises(failure, match="initialize failed"):
        normalize_python_index(index, tmp_path)

    assert len(shutdowns) == 1
    assert index.SerializeToString() == original


def test_resolver_provider_sees_only_the_standard_library(tmp_path, monkeypatch):
    from codenib import toolchains
    from codenib.graph.incremental import lsp_client

    index = _fixture(tmp_path)
    probes = []

    class ProbingClient:
        def __init__(self, *_args, path_prefix=None):
            self.path_prefix = path_prefix

        @staticmethod
        def get_lsp_command(_):
            return ["basedpyright-langserver", "--stdio"]

        def start(self, **_kwargs):
            # basedpyright asks python3 on PATH for its import search paths.
            env = {"PATH": os.pathsep.join([self.path_prefix, os.environ["PATH"]])}
            probe = subprocess.run(
                ["python3", "-c", "import json, sys; print(json.dumps(sys.path))"],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            probes.append((self.path_prefix, json.loads(probe.stdout)))
            raise RuntimeError("stop after the probe")

        def shutdown(self):
            pass

    monkeypatch.setattr(lsp_client, "LSPClient", ProbingClient)
    monkeypatch.setattr(toolchains, "resolve_command", lambda _: "/tools/basedpyright")
    with pytest.raises(RuntimeError, match="stop after the probe"):
        normalize_python_index(index, tmp_path)

    [(prefix, search_paths)] = probes
    assert not [path for path in search_paths if "-packages" in path]
    assert not Path(prefix).exists()


def test_expired_recovery_budget_does_not_start_a_provider(tmp_path, monkeypatch):
    index = _fixture(tmp_path)
    original = index.SerializeToString()
    monkeypatch.setattr(python_identity, "_REPAIR_TIMEOUT", 0)

    with pytest.raises(RuntimeError, match="exceeded its 120s deadline"):
        normalize_python_index(
            index,
            tmp_path,
            resolve_definitions=lambda *_: pytest.fail(
                "expired budget must stop lookup"
            ),
        )

    assert index.SerializeToString() == original


@pytest.mark.parametrize(
    "case",
    [
        "parameters",
        "rebinding",
        "noncyclic",
        "unicode",
        "local_bindings",
        "properties",
        "varargs",
        "field_redefinitions",
        "attribute_fields",
    ],
)
def test_native_decoder_preserves_recovered_definitions_and_references(tmp_path, case):
    pytest.importorskip("codenib_core")
    from codenib.scip_interface.scip_decode_core import SCIPDecoderCore

    index, resolve, _ = _captured_fixture(tmp_path, case)
    normalize_python_index(index, tmp_path, resolve_definitions=resolve)
    decoded = tmp_path / "index.decoded"
    decoded.write_text(MessageToString(index, as_utf8=True))
    serial = SCIPPythonGraphDecoder(str(decoded), str(tmp_path)).decode()
    native = SCIPDecoderCore(str(decoded), str(tmp_path), language="python").decode()

    def signature(graph):
        fields = (
            "type",
            "file",
            "start_line",
            "end_line",
            "selection_line",
            "unified_name",
            "symbol_kind",
            "has_definition",
        )
        vertices = {
            v["name"]: {key: v.attributes().get(key) for key in fields}
            for v in graph.graph.vs
        }
        edges = sorted(
            (
                graph.graph.vs[e.source]["name"],
                graph.graph.vs[e.target]["name"],
                e["type"],
                e["anchor_file"],
                e["anchor_line"],
            )
            for e in graph.graph.es
        )
        return vertices, edges

    assert signature(serial) == signature(native)


def test_native_module_alias_matches_serial_file_namespace_handling(tmp_path):
    pytest.importorskip("codenib_core")
    from codenib.scip_interface.scip_decode_core import SCIPDecoderCore

    # IPython references this alias from tests/test_magic.py:952.
    # The serial decoder treats `/__init__:profile.` as a module namespace;
    # the native decoder must not create an extra field after unification.
    index = Index()
    document = index.documents.add()
    document.relative_path = "pkg/module.py"
    document = index.documents.add()
    document.relative_path = "tests/test_magic.py"
    caller = document.occurrences.add()
    caller.range.extend([947, 4, 16])
    caller.symbol = "scip-python python fixture 1 `tests.test_magic`/test_profile()."
    caller.symbol_roles = 1
    caller.enclosing_range.extend([947, 0, 955, 0])
    occurrence = document.occurrences.add()
    occurrence.range.extend([951, 22, 29])
    occurrence.symbol = "scip-python python fixture 1 `pkg.module`/__init__:profile."
    occurrence.symbol_roles = 8
    decoded = tmp_path / "index.decoded"
    decoded.write_text(MessageToString(index, as_utf8=True))

    serial = SCIPPythonGraphDecoder(str(decoded), str(tmp_path)).decode()
    native = SCIPDecoderCore(str(decoded), str(tmp_path), language="python").decode()

    assert set(serial.name_to_vertex) == set(native.name_to_vertex)
    assert all("__init__:profile" not in name for name in native.name_to_vertex)
    for graph in [serial, native]:
        assert {
            (graph.graph.vs[e.source]["name"], graph.graph.vs[e.target]["name"])
            for e in graph.graph.es
            if e["type"] == EDGE_TYPE_REFERENCE
        } == {("tests/test_magic.py:test_profile()", "pkg/module.py")}
