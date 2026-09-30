# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

import pytest

from codenib.wiki.agent_wiki import AgentWiki
from codenib.wiki.source_excerpt import (
    EXCERPT_VERSION,
    GAP,
    anchor_excerpts,
    segments,
    signature_span,
)

GO = [
    "// insertChild adds a node.",
    "func (n *node) insertChild(path string) {",
    "\tfor {",
    "\t\twildcard, i, valid := findWildcard(path)",
    "\t\tif i < 0 {",
    "\t\t\tbreak",
    "\t\t}",
    "",
    "\t\tchild := &node{",
    "\t\t\tpath: wildcard,",
    "\t\t}",
    "\t\tn.addChild(child)",
    "\t}",
    "}",
]


@pytest.mark.parametrize(
    "lines, span",
    [
        (GO, (1, 1)),
        (["@property", "def name(self):", "    return 1"], (0, 2)),
        (["@decorator", "def long(", "    a,", "    b,", "):", "    pass"], (1, 1)),
        (
            [
                "@decorator(",
                "    setting=True,",
                ")",
                "async def run(",
                *["    arg," for _ in range(12)],
                "):",
                "    pass",
            ],
            (3, 1),
        ),
        (["def f(", "    a,", "    b,", ") -> None:", "    pass"], (0, 4)),
        (["int", "main(void)", "{", "  return 0;", "}"], (0, 3)),
        (["fn run<T>()", "where", "    T: Send,", "{", "}"], (0, 4)),
        (["const f = (a) => {", "  return a;", "};"], (0, 1)),
        (["x = compute()", "y = x"], (0, 1)),
        (
            ["def resolve(", *[f"    arg{i}," for i in range(9)], "):", "    pass"],
            (0, 1),
        ),
        ([], (0, 0)),
    ],
)
def test_signature_span_ends_at_the_body_opener(lines, span):
    assert signature_span(lines) == span


def test_window_inside_a_definition_keeps_the_signature_and_real_lines():
    parts = segments(GO, 288, 8, 12)
    assert parts == [(289, [GO[1]]), (296, GO[8:12])]


def test_window_near_the_signature_is_shown_whole_instead_of_a_tiny_gap():
    assert segments(GO, 288, 3, 6) == [(289, GO[1:6])]


def test_file_evidence_has_no_signature_row():
    assert segments(GO, 288, 8, 12, definition=False) == [(296, GO[8:12])]


def read_from(lines, first=288):
    def read_lines(file, start, end):
        assert file == "tree.go"
        return lines[start - first : end - first + 1]

    return read_lines


EVIDENCE = [
    {
        "id": "E2",
        "file": "tree.go",
        "start_line": 288,
        "end_line": 301,
        "kind": "method",
    }
]


def test_legacy_excerpt_gains_signature_gap_and_absolute_marks():
    # The generator drops blank lines and numbers marks within the window.
    markdown = (
        "Intro.\n\n```go hl=1,4\n\t\tchild := &node{\n\t\t\tpath: wildcard,\n"
        "\t\t}\n\t\tn.addChild(child)\n```\n\n"
        "Source excerpt from `node.insertChild()`. [E2](#evidence-E2)\n"
    )
    out = anchor_excerpts(markdown, EVIDENCE, read_from(GO))
    assert (
        "```go at=289,296 hl=296,299\n"
        "func (n *node) insertChild(path string) {\n"
        f"{GAP}\n"
        "\t\tchild := &node{\n\t\t\tpath: wildcard,\n\t\t}\n\t\tn.addChild(child)\n```"
    ) in out
    assert out.startswith("Intro.\n\n") and out.endswith("[E2](#evidence-E2)\n")


def test_highlight_ranges_are_bounded_by_the_actual_excerpt():
    markdown = (
        "```go hl=1-999999999999999999999999\n"
        "\t\tchild := &node{\n\t\t\tpath: wildcard,\n```\n\n"
        "Source excerpt. [E2](#evidence-E2)"
    )
    out = anchor_excerpts(markdown, EVIDENCE, read_from(GO))
    assert "hl=296,297" in out


def test_saved_visitor_excerpts_use_saved_source_without_changing_prose(tmp_path):
    from codenib.storage import SQLiteWikiStore
    from codenib.wiki.visitor_wiki import VisitorWikis

    store = SQLiteWikiStore(tmp_path / "wiki.sqlite3")
    attempt = "a" * 64
    store.publish(
        entry_id=f"visitor:{attempt}", repository_id="attempts", envelope={"data": {}}
    )
    original = {
        "id": "routing",
        "title": "Routing",
        "markdown": (
            "```go hl=1\n\t\tchild := &node{\n\t\t\tpath: wildcard,\n```\n\n"
            "Source excerpt. [E2](#evidence-E2)"
        ),
        "evidence": {"items": EVIDENCE},
        "citations": [
            {
                "file": "tree.go",
                "start_line": 288,
                "end_line": 301,
                "content": "\n".join(GO),
                "type": "method",
            }
        ],
    }
    page_id = f"visitor:{attempt}:page:routing"
    store.publish(entry_id=page_id, repository_id=attempt, envelope={"data": original})
    wikis = VisitorWikis(store)
    for _ in range(2):
        rendered = wikis.page(attempt, "routing")
        assert "```go at=289,296 hl=296" in rendered["markdown"]
        assert "func (n *node) insertChild" in rendered["markdown"]
        assert rendered["citations"] == original["citations"]
        assert store.read(page_id).envelope["data"] == original


@pytest.mark.parametrize(
    "fence",
    [
        "```mermaid\nflowchart LR\n```",
        "```go at=289\nfunc (n *node) insertChild(path string) {\n```",
        "```go\nnot in the source\n```",
    ],
)
def test_diagrams_anchored_and_unknown_windows_are_left_alone(fence):
    markdown = f"{fence}\n\nSource excerpt. [E2](#evidence-E2)"
    assert anchor_excerpts(markdown, EVIDENCE, read_from(GO)) == markdown


def test_cached_page_is_upgraded_once_without_touching_prose():
    calls = []

    def source(file, start, end):
        calls.append((file, start, end))
        return {"start_line": start, "content": "\n".join(GO[start - 288 :])}

    wiki = SimpleNamespace(_wb=SimpleNamespace(source=source))
    wiki._anchor_source_excerpts = lambda page: AgentWiki._anchor_source_excerpts(
        wiki, page
    )
    page = {
        "id": "radix",
        "markdown": "```go\n\t\tn.addChild(child)\n```\n\nSource excerpt. [E2]",
        "evidence": {"items": EVIDENCE},
        "citations": [{"file": "tree.go"}],
    }
    upgraded = AgentWiki._refresh_source_excerpts(wiki, page)
    # A new dict tells the reader to write the upgraded page back to the cache.
    assert upgraded is not page and "excerpt_version" not in page
    assert upgraded["excerpt_version"] == EXCERPT_VERSION
    assert upgraded["markdown"].startswith("```go at=289,299\nfunc (n *node)")
    assert upgraded["citations"] == page["citations"]
    assert calls == [("tree.go", 288, 301)]
    assert AgentWiki._refresh_source_excerpts(wiki, upgraded) is upgraded
    assert len(calls) == 1
