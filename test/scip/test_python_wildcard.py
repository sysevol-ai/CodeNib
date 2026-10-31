# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Retarget actual compiler module misbindings using controlled LSP evidence."""

import copy
from pathlib import Path

import pytest
from google.protobuf.text_format import MessageToString, Parse

from codenib.scip_interface import python_identity
from codenib.scip_interface.lsp_occurrence_index import SCIPOccurrenceIndex
from codenib.scip_interface.python_identity import normalize_python_index
from codenib.scip_interface.scip_decode_python import SCIPPythonGraphDecoder
from codenib.scip_interface.scip_pb2 import Index
from codenib.types import EDGE_TYPE_REFERENCE

CAPTURES = Path(__file__).parents[1] / "fixtures/scip_python_wildcard"


def fixture(root):
    files = {
        "wildcard.py": "consumer",
        "pkg/bridge.py": "bridge",
        "pkg/origin.py": "origin",
    }
    for file, label in files.items():
        path = root / file
        path.parent.mkdir(exist_ok=True)
        path.write_bytes((CAPTURES / f"wildcard_import.{label}.source").read_bytes())
    return Parse((CAPTURES / "wildcard_import.decoded").read_text(), Index())


def target(root, file="pkg/origin.py", line=0, start=4, end=10):
    return {
        "uri": (root / file).as_uri(),
        "range": {
            "start": {"line": line, "character": start},
            "end": {"line": line, "character": end},
        },
    }


def consumer(index):
    return next(doc for doc in index.documents if doc.relative_path == "wildcard.py")


def call(index):
    return next(o for o in consumer(index).occurrences if tuple(o.range[:2]) == (1, 8))


def test_captured_star_import_read_is_not_a_reference_to_the_bridge_module(tmp_path):
    index = fixture(tmp_path)
    assert call(index).symbol.endswith("`pkg.bridge`/__init__:")
    queries = []

    def resolve(file, line, character):
        queries.append((file, line, character))
        return [target(tmp_path)]

    report = normalize_python_index(index, tmp_path, resolve_definitions=resolve)

    assert call(index).symbol.endswith("`pkg.origin`/target().")
    assert call(index).symbol_roles == 8
    assert queries == [("wildcard.py", 1, 8)]
    assert len(report["recovered_wildcard_references"]) == 1
    assert report["unresolved_wildcard_references"] == []
    # Keep the real import edge and the unsupported compiler assignment role.
    # Neither a new occurrence nor a guessed variable declaration is invented.
    remaining = [o for o in consumer(index).occurrences if o is not call(index)]
    assert len(consumer(index).occurrences) == 4
    assert sum(o.symbol.endswith("`pkg.bridge`/__init__:") for o in remaining) == 2
    before = index.SerializeToString()
    queries.clear()
    normalize_python_index(index, tmp_path, resolve_definitions=resolve)
    assert index.SerializeToString() == before and queries == []


@pytest.mark.parametrize("failure", ("empty", "external", "short", "mixed"))
def test_unconfirmed_wildcard_target_preserves_and_reports_the_original(
    tmp_path, failure
):
    index = fixture(tmp_path)
    good = target(tmp_path)
    invalid = target(tmp_path, file="unindexed.py")
    replies = {
        "empty": [],
        "external": [invalid],
        "short": [target(tmp_path, end=9)],
        "mixed": [good, invalid],
    }

    report = normalize_python_index(
        index, tmp_path, resolve_definitions=lambda *_: replies[failure]
    )

    assert call(index).symbol.endswith("`pkg.bridge`/__init__:")
    assert report["recovered_wildcard_references"] == []
    assert len(report["unresolved_wildcard_references"]) == 1
    assert len(consumer(index).occurrences) == 4


def test_wildcard_read_preserves_all_declarations_of_the_selected_binding(tmp_path):
    index = fixture(tmp_path)
    path = tmp_path / "pkg/origin.py"
    path.write_text(path.read_text() + "target = 0\n")
    doc = next(d for d in index.documents if d.relative_path == "pkg/origin.py")
    original = next(o for o in doc.occurrences if tuple(o.range[:2]) == (0, 4))
    assignment = doc.occurrences.add()
    assignment.CopyFrom(original)
    assignment.range[:] = [2, 0, 6]
    assignment.symbol_roles = 8

    normalize_python_index(
        index, tmp_path, resolve_definitions=lambda *_: [target(tmp_path)]
    )

    selected = [o for o in consumer(index).occurrences if tuple(o.range[:2]) == (1, 8)]
    assert len(selected) == 2
    assert any(o.symbol.endswith("/target().") for o in selected)
    assert any("target@codenib-l2-c0" in o.symbol for o in selected)


def test_source_edit_after_wildcard_reply_keeps_normalization_atomic(tmp_path):
    index = fixture(tmp_path)
    before = index.SerializeToString()

    def resolve(*_args):
        (tmp_path / "pkg/origin.py").write_text("def changed():\n    return 9\n")
        return [copy.deepcopy(target(tmp_path))]

    with pytest.raises(RuntimeError, match="source changed during identity recovery"):
        normalize_python_index(index, tmp_path, resolve_definitions=resolve)
    assert index.SerializeToString() == before


def test_non_wildcard_module_reads_do_not_start_a_definition_provider(tmp_path):
    index = fixture(tmp_path)
    path = tmp_path / "wildcard.py"
    path.write_text(path.read_text().replace("import *", "import exported"))

    def unexpected(*_args):
        raise AssertionError("non-wildcard reads must keep the compiler route")

    report = normalize_python_index(index, tmp_path, resolve_definitions=unexpected)
    assert call(index).symbol.endswith("`pkg.bridge`/__init__:")
    assert report["recovered_wildcard_references"] == []
    assert report["unresolved_wildcard_references"] == []


def test_module_repair_preserves_other_compiler_targets_at_the_same_position(tmp_path):
    index = fixture(tmp_path)
    extra = consumer(index).occurrences.add()
    extra.CopyFrom(call(index))
    extra.symbol = (
        next(doc for doc in index.documents if doc.relative_path == "pkg/bridge.py")
        .symbols[-1]
        .symbol
    )
    retained_symbol = extra.symbol

    report = normalize_python_index(
        index, tmp_path, resolve_definitions=lambda *_: [target(tmp_path)]
    )
    at_position = [
        o.symbol for o in consumer(index).occurrences if tuple(o.range[:2]) == (1, 8)
    ]
    assert len(at_position) == 2
    assert retained_symbol in at_position
    assert len(report["recovered_wildcard_references"]) == 1


def test_exhausted_recovery_budget_reports_the_unqueried_original(
    tmp_path, monkeypatch
):
    index = fixture(tmp_path)
    monkeypatch.setattr(python_identity, "_REPAIR_TIMEOUT", 0)

    def unexpected(*_args):
        raise AssertionError("an exhausted budget must not start a provider")

    report = normalize_python_index(index, tmp_path, resolve_definitions=unexpected)
    assert call(index).symbol.endswith("`pkg.bridge`/__init__:")
    assert report["unresolved_wildcard_references"][0]["reason"] == "repair_deadline"


def test_wildcard_query_converts_utf8_source_spans_to_utf16_provider_columns(tmp_path):
    index = fixture(tmp_path)
    path = tmp_path / "wildcard.py"
    prefix = 'marker = "😀"; '
    path.write_text(path.read_text().replace("value =", prefix + "value ="))
    width = len(prefix.encode("utf-16-le")) // 2
    for occurrence in consumer(index).occurrences:
        if occurrence.range[0] == 1:
            occurrence.range[1] += width
            occurrence.range[-1] += width
    queries = []

    def resolve(file, line, character):
        queries.append((file, line, character))
        return [target(tmp_path)]

    normalize_python_index(index, tmp_path, resolve_definitions=resolve)
    assert queries == [("wildcard.py", 1, 8 + width)]
    matching = [
        o for o in consumer(index).occurrences if o.symbol.endswith("/target().")
    ]
    assert len(matching) == 1
    assert list(matching[0].range) == [
        1,
        8 + len(prefix.encode()),
        1,
        16 + len(prefix.encode()),
    ]


@pytest.mark.parametrize("backend", ("python", "core"))
def test_recovered_wildcard_edge_and_position_survive_both_decoders(tmp_path, backend):
    index = fixture(tmp_path)
    normalize_python_index(
        index, tmp_path, resolve_definitions=lambda *_: [target(tmp_path)]
    )
    decoded = tmp_path / "index.decoded"
    decoded.write_text(MessageToString(index, as_utf8=True))
    if backend == "core":
        pytest.importorskip("codenib_core")
        from codenib.scip_interface.scip_decode_core import SCIPDecoderCore

        graph = SCIPDecoderCore(str(decoded), str(tmp_path), language="python").decode()
    else:
        graph = SCIPPythonGraphDecoder(str(decoded), str(tmp_path)).decode()
    assert any(
        edge["type"] == EDGE_TYPE_REFERENCE
        and graph.graph.vs[edge.source]["name"] == "wildcard.py"
        and graph.graph.vs[edge.target]["name"] == "pkg/origin.py:target()"
        and edge["anchor_file"] == "wildcard.py"
        and edge["anchor_line"] == 1
        for edge in graph.graph.es
    )
    positions = SCIPOccurrenceIndex.from_scip_index(index)
    definitions = positions.definitions(file_path="wildcard.py", line=1, character=8)
    assert len(definitions) == 1
    assert definitions[0].file_path == "pkg/origin.py"
    assert definitions[0].start_line == 0
