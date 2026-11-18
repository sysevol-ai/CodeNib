# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for what counts as a test, and how to run it, in each language."""

from __future__ import annotations

from codenib.guard.impact import ImpactGraph
from codenib.guard.languages import (
    GO,
    PYTHON,
    RUST,
    convention_for,
    is_test_definition,
    languages_in,
)
from codenib.guard.languages import test_commands as commands_for
from codenib.guard.languages import test_id as runner_id


def test_languages_are_found_from_file_names():
    names = ["a.py", "cmd/main.go", "web/app.tsx", "types.d.ts", "README.md"]

    assert languages_in(names) == ["python", "go", "typescript"]
    # A declaration file holds no code to guard.
    assert convention_for("types.d.ts") is None
    assert convention_for("notes.txt") is None


def test_each_language_marks_its_tests_its_own_way():
    rust = {
        "src/lib.rs": [
            "fn helper() {}",
            "",
            "#[cfg(test)]",
            "mod tests {",
            "    #[test]",
            "    fn adds() {}",
            "    #[tokio::test]",
            "    async fn waits() {}",
            "}",
        ]
    }
    lines = lambda path: rust.get(path, [])  # noqa: E731

    assert is_test_definition(PYTHON, "test_total", "tests/test_cart.py", 3, lines)
    assert not is_test_definition(PYTHON, "helper", "tests/test_cart.py", 3, lines)
    assert is_test_definition(GO, "TestRouter", "gin/router_test.go", 9, lines)
    assert is_test_definition(GO, "BenchmarkRouter", "gin/router_test.go", 9, lines)
    # ``Testing`` is a helper, and a test outside a _test.go file is not run.
    assert not is_test_definition(GO, "Testing", "gin/router_test.go", 9, lines)
    assert not is_test_definition(GO, "TestRouter", "gin/router.go", 9, lines)
    assert is_test_definition(RUST, "adds", "src/lib.rs", 5, lines)
    assert is_test_definition(RUST, "waits", "src/lib.rs", 7, lines)
    assert not is_test_definition(RUST, "helper", "src/lib.rs", 0, lines)


def test_commands_follow_each_language_s_runner():
    ids = [
        "tests/test_cart.py::test_total",
        "gin/router_test.go::TestRouter",
        "gin/router_test.go::TestGroup",
        "binding/form_test.go::TestForm",
        "src/lib.rs::adds",
        "web/cart.test.ts",
    ]

    assert commands_for(ids, limit=20) == [
        "python -m pytest tests/test_cart.py::test_total",
        "go test ./gin -run '^(TestRouter|TestGroup)$' && "
        "go test ./binding -run '^(TestForm)$'",
        "cargo test --workspace -- adds",
    ]
    assert runner_id(GO, "gin/router_test.go:TestRouter") == (
        "gin/router_test.go::TestRouter"
    )


def test_go_and_javascript_tests_are_reached(make_graph, tmp_path):
    nodes = [
        ("gin/router.go:handle()", "function", "gin/router.go", 0, 3),
        ("gin/router_test.go:TestRouter()", "function", "gin/router_test.go", 0, 3),
        ("gin/router_test.go:setup()", "function", "gin/router_test.go", 5, 7),
        ("web/cart.js:total()", "function", "web/cart.js", 0, 2),
        # JavaScript tests are callbacks: the file vertex makes the reference.
        ("web/cart.test.js", "file", None, None, None),
        ("web/app.js", "file", None, None, None),
    ]
    edges = [
        ("gin/router_test.go:TestRouter()", "gin/router.go:handle()", "reference", 1),
        ("gin/router_test.go:setup()", "gin/router.go:handle()", "reference", 6),
        ("web/cart.test.js", "web/cart.js:total()", "reference", 4),
        ("web/app.js", "web/cart.js:total()", "reference", 2),
    ]
    graph = ImpactGraph(make_graph(nodes, edges), tmp_path)

    impact = graph.impact({"gin/router.go": {2}, "web/cart.js": {1}})

    assert sorted(t.test_id for t in impact.tests) == [
        "gin/router_test.go::TestRouter",
        "web/cart.test.js",
    ]
    assert impact.total_tests == 2


def _decoded_go_graph(root):
    """A graph from the Go decoder itself, so the names are the ones it makes."""

    from codenib.scip_interface.scip_decode_go import SCIPGoGraphDecoder
    from codenib.scip_interface.scip_indexer_base import index_text
    from codenib.scip_interface.scip_pb2 import Index

    (root / "go.mod").write_text("module example.com/proj\n", encoding="utf-8")
    rows = {
        "binding/form.go": [("Bind().", 2, 1)],
        "binding/form_test.go": [
            ("TestForm().", 4, 1),
            ("Bind().", 5, 0),
            ("Suite#TestRun().", 8, 1),
            ("BenchmarkForm().", 12, 1),
        ],
    }
    index = Index()
    for path, occurrences in rows.items():
        document = index.documents.add(relative_path=path)
        for descriptor, line, roles in occurrences:
            occurrence = document.occurrences.add(
                symbol="scip-go gomod example.com/proj v1 "
                f"`example.com/proj/binding`/{descriptor}",
                symbol_roles=roles,
            )
            occurrence.range.extend([line, 5, 9])
    decoded = root / "index.decoded"
    decoded.write_text(index_text(index), encoding="utf-8")
    return SCIPGoGraphDecoder(str(decoded), project_root=str(root)).decode().graph


def test_go_tests_in_a_subpackage_are_tests(tmp_path):
    graph = _decoded_go_graph(tmp_path)
    # The decoder's key carries the package; only the unified name has a path.
    assert "binding/TestForm" in graph.vs["name"]

    guard = ImpactGraph(graph, tmp_path)

    # Suite.TestRun is a method, which ``go test`` does not run.
    assert sorted(guard._test_id(test) for test in guard.tests) == [
        "binding/form_test.go::BenchmarkForm",
        "binding/form_test.go::TestForm",
    ]


def test_a_rust_test_attribute_belongs_to_one_function():
    source = [
        "pub fn two() -> i32 { 2 }",
        "",
        "#[cfg(test)]",
        "mod tests {",
        "    fn before() -> i32 { 1 }",
        "    /// Documented, with a fn in the comment.",
        "    #[test]",
        "    #[should_panic]",
        "    fn adds() {",
        "    }",
        "    fn after() -> i32 { 2 }",
        "    #[test] // why",
        "    fn inline() {} // done",
        "    fn last() {}",
        "}",
    ]
    lines = lambda path: source  # noqa: E731

    def is_test(start_line):
        return is_test_definition(RUST, "name", "src/lib.rs", start_line, lines)

    # rust-analyzer starts a definition at its first attribute or doc comment.
    assert is_test(5) and is_test(11)
    # Without enclosing ranges a definition starts at its ``fn`` line.
    assert is_test(8) and is_test(12)
    for helper in (0, 4, 10, 13):
        assert not is_test(helper)


def test_go_benchmarks_get_the_flag_that_runs_them():
    ids = [
        "gin/router_test.go::TestRouter",
        "gin/router_test.go::BenchmarkRouter",
        "binding/form_test.go::BenchmarkForm",
    ]

    assert commands_for(ids, limit=20) == [
        "go test ./gin -run '^(TestRouter)$' -bench '^(BenchmarkRouter)$' && "
        "go test ./binding -run '^$' -bench '^(BenchmarkForm)$'"
    ]


def test_a_rewritten_javascript_test_file_is_an_edited_test(make_graph, tmp_path):
    # As the builder makes them: a file vertex has a name and a type only.
    nodes = [
        ("web/cart.js:total()", "function", "web/cart.js", 0, 2),
        ("web/cart.test.js", "file", None, None, None),
        ("web/only.test.js", "file", None, None, None),
    ]
    edges = [("web/cart.test.js", "web/cart.js:total()", "reference", 4)]
    graph = ImpactGraph(make_graph(nodes, edges), tmp_path)
    changed = {"web/cart.js": {1}, "web/cart.test.js": {5}, "web/only.test.js": {2}}

    rewritten = graph.impact(
        changed,
        rewritten_lines={"web/cart.js": {1}, "web/cart.test.js": {5}},
    )

    assert rewritten.edited_tests == ["web/cart.test.js"]
    assert rewritten.unmapped_files == []
    assert [t.test_id for t in rewritten.source_tests] == ["web/cart.test.js"]

    # A test only added to the file leaves it a test that calls the change.
    added = graph.impact(changed, rewritten_lines={"web/cart.js": {1}})

    assert added.edited_tests == []
    assert [(t.test_id, t.depth) for t in added.tests] == [("web/cart.test.js", 1)]
