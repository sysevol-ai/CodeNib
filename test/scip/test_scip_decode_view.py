# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Graph decoders read the same values from the parsed index as from its text."""

from __future__ import annotations

import pickle

import pytest

from codenib.scip_interface.scip_decode_go import SCIPGoGraphDecoder
from codenib.scip_interface.scip_decode_python import SCIPPythonGraphDecoder
from codenib.scip_interface.scip_decode_rust import SCIPRustGraphDecoder
from codenib.scip_interface.scip_decode_ts import SCIPTypeScriptGraphDecoder
from codenib.scip_interface.scip_decode_view import (
    document_path,
    document_path_from_text,
    occurrence_view,
    occurrence_view_from_text,
    symbol_view,
    symbol_view_from_text,
    text_blocks_match,
)
from codenib.scip_interface.scip_indexer_base import index_text
from codenib.scip_interface.scip_pb2 import (
    Document,
    Index,
    Occurrence,
    SymbolInformation,
)
from codenib.scip_interface.typescript_semantics import _TS_SIGNATURE_RE

# Strings whose text form reads differently from the value.
AWKWARD = [
    'say "hi"',
    "it's",
    "back\\slash",
    "line\nbreak\ttab\rcr",
    "\x01\x7f",
    "café 中",
    "range: 12",
    "symbol_roles: 3",
    "enclosing_range: 4",
    "python-stdlib 3.11",
    "",
]


def _occurrence(**fields) -> Occurrence:
    occurrence = Occurrence()
    for name, value in fields.items():
        field = getattr(occurrence, name)
        if isinstance(value, list):
            field.extend(value)
        else:
            setattr(occurrence, name, value)
    return occurrence


OCCURRENCES = [
    _occurrence(range=[1, 2, 5], symbol="pkg 0 m/f().", symbol_roles=1),
    _occurrence(range=[1, 2, 3, 5], symbol="pkg 0 m/f()."),
    _occurrence(range=[-1, 2, 5], symbol="pkg 0 m/f().", symbol_roles=-4),
    _occurrence(range=[1, 2], symbol="local 3", enclosing_range=[0, 0, 9, 1]),
    _occurrence(
        range=[4, 0, 4, 2],
        symbol="pkg 0 m/C#",
        symbol_roles=1,
        enclosing_range=[3, 0, -2, 7],
    ),
    _occurrence(range=[1, 2, 5], symbol_roles=2),
    _occurrence(
        range=[1, 2, 5],
        symbol="pkg 0 m/f().",
        override_documentation=["doc range: 9 symbol_roles: 1"],
    ),
    _occurrence(range=[1, 2, 5], symbol="pkg 0 m/f().", syntax_kind=999),
] + [
    _occurrence(range=[1, 2, 5], symbol=f"pkg 0 m/{text}().", symbol_roles=1)
    for text in AWKWARD
]
_with_diagnostic = _occurrence(range=[7, 1, 3], symbol="python-stdlib 3 os/x.")
_with_diagnostic.diagnostics.add(message="range: 3 symbol_roles: 1 python-stdlib")
OCCURRENCES.append(_with_diagnostic)


@pytest.mark.parametrize("occurrence", OCCURRENCES)
def test_occurrence_view_matches_its_text(occurrence):
    assert occurrence_view(occurrence) == occurrence_view_from_text(
        index_text(occurrence)
    )


@pytest.mark.parametrize("path", ["src/a.py", *[f"d/{text}.py" for text in AWKWARD]])
def test_document_path_matches_its_text(path):
    document = Document(relative_path=path)
    assert document_path(document) == document_path_from_text(index_text(document))


def _symbol(**fields) -> SymbolInformation:
    info = SymbolInformation()
    for name, value in fields.items():
        field = getattr(info, name)
        if isinstance(value, list):
            field.extend(value)
        else:
            setattr(info, name, value)
    return info


SIGNATURE = "```ts\nclass Foo {}\n```"
_nested = _symbol(symbol="m/X#", documentation=[SIGNATURE])
_nested.signature_documentation.symbols.add(symbol="m/Y#", kind=7)
SYMBOLS = [
    _symbol(symbol="m/X#", kind=7, documentation=[SIGNATURE]),
    _symbol(symbol="m/X#", documentation=[SIGNATURE, "second"]),
    _symbol(symbol="m/X#", kind=5000, documentation=["```typescript\nenum E"]),
    _symbol(symbol="m/X#", documentation=['"quoted" ```ts\nclass']),
    _symbol(symbol='m/"q"#', documentation=[SIGNATURE]),
    _symbol(symbol="m/X#", documentation=["x" * 500]),
    _symbol(documentation=[SIGNATURE]),
    _nested,
]


@pytest.mark.parametrize("info", SYMBOLS)
def test_symbol_view_matches_its_text(info):
    from_index = symbol_view(info)
    from_text = symbol_view_from_text(index_text(info))
    assert from_index.symbol == from_text.symbol
    assert from_index.kind == from_text.kind
    # The parsed-index view keeps a prefix of the documentation, enough for
    # the only pattern that reads it.
    assert (from_index.documentation is None) == (from_text.documentation is None)
    if from_index.documentation is not None:
        assert from_text.documentation.startswith(from_index.documentation)
        index_match = _TS_SIGNATURE_RE.search(from_index.documentation)
        text_match = _TS_SIGNATURE_RE.search(from_text.documentation)
        assert (index_match and index_match.group(1)) == (
            text_match and text_match.group(1)
        )


def _index(language: str, extension: str) -> Index:
    """Every occurrence shape above, in two documents, for one language."""

    tool = {
        "python": "scip-python python pkg 0.1",
        "go": "scip-go gomod example.com/pkg v0.1",
        "rust": "rust-analyzer cargo pkg 0.1.0",
        "ts": "scip-typescript npm pkg 0.1.0",
    }[language]
    index = Index()
    index.metadata.text_document_encoding = 1
    for name in ("src/a", 'src/sp ace "q"'):
        document = index.documents.add(relative_path=f"{name}.{extension}")
        for number, template in enumerate(OCCURRENCES):
            occurrence = document.occurrences.add()
            occurrence.CopyFrom(template)
            if occurrence.symbol and not occurrence.symbol.startswith("local "):
                occurrence.symbol = f"{tool} `src.a`/{occurrence.symbol}{number}"
        definition = document.occurrences.add(
            symbol=f"{tool} `src.a`/Thing#", symbol_roles=1
        )
        definition.range.extend([2, 0, 5])
        definition.enclosing_range.extend([2, 0, 9, 0])
        document.symbols.add(symbol=f"{tool} `src.a`/Thing#", documentation=[SIGNATURE])
    return index


DECODERS = [
    (SCIPPythonGraphDecoder, "python", "py"),
    (SCIPGoGraphDecoder, "go", "go"),
    (SCIPRustGraphDecoder, "rust", "rs"),
    (SCIPTypeScriptGraphDecoder, "ts", "ts"),
]


def _graph_state(graph) -> tuple:
    g = graph.graph
    vertices = [sorted(v.attributes().items(), key=repr) for v in g.vs]
    edges = [(e.source, e.target, sorted(e.attributes().items())) for e in g.es]
    return pickle.dumps((vertices, edges)), g.vcount(), g.ecount()


@pytest.mark.parametrize("decoder, language, extension", DECODERS)
def test_decoders_build_the_same_graph_from_the_index(
    tmp_path, decoder, language, extension
):
    index = _index(language, extension)
    text = index_text(index)
    assert text_blocks_match(index, text)
    decoded = tmp_path / "index.decoded"
    decoded.write_text(text, encoding="utf-8")
    from_text = decoder(str(decoded), project_root=str(tmp_path)).decode()
    from_index = decoder(
        str(decoded), project_root=str(tmp_path), scip_index=index
    ).decode()
    state, vertices, edges = _graph_state(from_text)
    assert vertices > 3 and edges > 2  # the fixture reaches symbol nodes
    assert _graph_state(from_index) == (state, vertices, edges)


def test_blocks_match_a_plain_index():
    index = _index("python", "py")
    assert text_blocks_match(index, index_text(index))


def test_a_keyword_in_a_string_matters_only_before_a_brace():
    index = _index("python", "py")
    index.documents[0].symbols[0].documentation.append("occurrences and symbols")
    assert text_blocks_match(index, index_text(index))
    index.documents[0].symbols[0].documentation.append("documents { x }")
    assert not text_blocks_match(index, index_text(index))


def test_a_document_without_a_path_is_read_from_the_text():
    index = _index("python", "py")
    index.documents.add()
    assert not text_blocks_match(index, index_text(index))


def test_a_signature_document_with_occurrences_is_read_from_the_text():
    index = _index("python", "py")
    nested = index.documents[0].symbols[0].signature_documentation
    nested.occurrences.add(symbol="x", range=[0, 0, 1])
    assert not text_blocks_match(index, index_text(index))
