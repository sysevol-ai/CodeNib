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
        "cargo test -- adds",
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
