# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The query-backed node search must equal the Python depth-first walk."""

import random

import pytest

from codenib.code_chunking import CppCodeChunker

TYPES = ("function_definition", "class_specifier")

SOURCES = {
    "nested": """
namespace ns {
struct Outer {
  int method() { struct Local { void inner() {} }; return 1; }
  class Inner { public: Inner() {} ~Inner() {} };
};
template <typename T> T twice(T value) { return value + value; }
}  // namespace ns
extern "C" { int c_entry(void) { return 0; } }
#if FEATURE
void feature(void) {}
#else
void feature(void) { int x = 0; (void)x; }
#endif
""",
    "macro_heavy_c": """
#define DECLARE(name) int name(void)
DECLARE(first) { return 1; }
static int second(int a,
                  int b) { return a + b; }
typedef struct node { struct node *next; } node_t;
int main(int argc, char **argv) { return second(argc, 0); }
""",
    "broken": "class A { void f( { int g() { return 1; } struct B { void h() {} ",
    "empty": "",
}


def _walk(chunker, root):
    return {t: [n.id for n in chunker._find_nodes_by_type(root, t)] for t in TYPES}


def _query(chunker, root):
    found = chunker._find_nodes_by_types(root, TYPES)
    return {t: [n.id for n in found[t]] for t in TYPES}


@pytest.fixture(scope="module")
def chunker():
    return CppCodeChunker()


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_query_matches_depth_first_walk(chunker, name):
    tree = chunker.parser.parse(SOURCES[name].encode("utf-8"))
    assert chunker._node_type_query(TYPES) is not None
    assert _query(chunker, tree.root_node) == _walk(chunker, tree.root_node)


def test_query_matches_walk_on_mutated_sources(chunker):
    rng = random.Random(20261107)
    base = SOURCES["nested"] + SOURCES["macro_heavy_c"]
    for _ in range(200):
        text = list(base)
        for _ in range(rng.randrange(1, 12)):
            position = rng.randrange(len(text) + 1)
            if rng.random() < 0.5 and text:
                del text[min(position, len(text) - 1)]
            else:
                text.insert(position, rng.choice("{}();<>#\n"))
        tree = chunker.parser.parse("".join(text).encode("utf-8"))
        assert _query(chunker, tree.root_node) == _walk(chunker, tree.root_node)


def test_query_on_subtree_stays_inside_it(chunker):
    tree = chunker.parser.parse(SOURCES["nested"].encode("utf-8"))
    [outer] = chunker._find_nodes_by_type(tree.root_node, "class_specifier")[:1]
    assert _query(chunker, outer) == _walk(chunker, outer)


def test_anonymous_or_unknown_types_use_the_walk(chunker):
    assert chunker._node_type_query(("{",)) is None
    assert chunker._node_type_query(("no_such_node_kind",)) is None
    tree = chunker.parser.parse(SOURCES["nested"].encode("utf-8"))
    found = chunker._find_nodes_by_types(tree.root_node, ("{",))
    walked = chunker._find_nodes_by_type(tree.root_node, "{")
    assert walked and [n.id for n in found["{"]] == [n.id for n in walked]


def test_ambiguous_spans_fall_back_to_the_walk(chunker, monkeypatch):
    tree = chunker.parser.parse(SOURCES["nested"].encode("utf-8"))
    root = tree.root_node
    calls = []
    original = chunker._find_nodes_by_type

    def recording_walk(node, node_type):
        calls.append(node_type)
        return original(node, node_type)

    class DuplicateCursor:
        did_exceed_match_limit = False

        def __init__(self, query):
            pass

        def captures(self, node):
            nodes = original(node, "function_definition")
            return {"node": nodes + nodes[:1]}

    monkeypatch.setattr("codenib.code_chunking.base.QueryCursor", DuplicateCursor)
    monkeypatch.setattr(chunker, "_find_nodes_by_type", recording_walk)
    found = chunker._find_nodes_by_types(root, TYPES)
    assert calls == list(TYPES)
    assert {t: [n.id for n in found[t]] for t in TYPES} == {
        t: [n.id for n in original(root, t)] for t in TYPES
    }


def test_cpp_definitions_unchanged_without_query(chunker, monkeypatch):
    source = SOURCES["nested"] + SOURCES["macro_heavy_c"]
    path_chunks = chunker.chunk_source(source, file_path="sample.cpp")
    monkeypatch.setattr(chunker, "_node_type_query", lambda node_types: None)
    assert chunker.chunk_source(source, file_path="sample.cpp") == path_chunks
