# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

import pytest

from codenib.wiki.agent_wiki import AgentWiki
from codenib.wiki.source_excerpt import (
    EXCERPT_VERSION,
    GAP,
    anchor_excerpts,
    focus_window,
    notice_terms,
    prose_lines,
    segments,
    signature_end,
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


def test_legacy_excerpt_gains_signature_gap_and_its_citation():
    # The generator drops blank lines and marked lines by word overlap; a
    # caption that names nothing in particular marks nothing.
    markdown = (
        "Intro.\n\n```go hl=1,4\n\t\tchild := &node{\n\t\t\tpath: wildcard,\n"
        "\t\t}\n\t\tn.addChild(child)\n```\n\n"
        "Source excerpt from `node.insertChild()`. [E2](#evidence-E2)\n"
    )
    out = anchor_excerpts(markdown, EVIDENCE, read_from(GO))
    assert (
        "```go at=289,296 cite=E2\n"
        "func (n *node) insertChild(path string) {\n"
        f"{GAP}\n"
        "\t\tchild := &node{\n\t\t\tpath: wildcard,\n\t\t}\n\t\tn.addChild(child)\n```"
    ) in out
    assert out.startswith("Intro.\n\n") and out.endswith("[E2](#evidence-E2)\n")


def test_generator_marks_are_not_carried_over():
    markdown = (
        "```go hl=1-999999999999999999999999\n"
        "\t\tchild := &node{\n\t\t\tpath: wildcard,\n```\n\n"
        "Source excerpt. [E2](#evidence-E2)"
    )
    out = anchor_excerpts(markdown, EVIDENCE, read_from(GO))
    assert "```go at=289,296 cite=E2\n" in out and " hl=" not in out


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
        assert "```go at=289,296 cite=E2\n" in rendered["markdown"]
        assert "func (n *node) insertChild" in rendered["markdown"]
        assert rendered["citations"] == original["citations"]
        assert store.read(page_id).envelope["data"] == original


@pytest.mark.parametrize(
    "fence",
    [
        "```mermaid\nflowchart LR\n```",
        "```go\nnot in the source\n```",
        # Anchored by an earlier version, but no longer the source's lines.
        "```go at=290\nfunc (n *node) insertChild(path string) {\n```",
    ],
)
def test_diagrams_and_unknown_windows_are_left_alone(fence):
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
    assert upgraded["markdown"].startswith("```go at=289,299 cite=E2\nfunc (n *node)")
    assert upgraded["citations"] == page["citations"]
    assert calls == [("tree.go", 288, 301)]
    assert AgentWiki._refresh_source_excerpts(wiki, upgraded) is upgraded
    assert len(calls) == 1


PY = [
    "def prepare_url(self, url, params):",
    '    """Prepares the given HTTP URL.',
    "",
    "    Raises when the URL is unusable.",
    '    """',
    "    url = url.strip()",
    "    # Don't prepare non-HTTP schemes such as `mailto`.",
    '    if ":" in url and not url.lower().startswith("http"):',
    "        self.url = url",
    "        return",
    "",
    "    try:",
    "        scheme, host = parse_url(url)",
    "    except LocationParseError as e:",
    "        raise InvalidURL(*e.args)",
    "",
    "    # A missing scheme is the common mistake.",
    "    if not scheme:",
    "        raise MissingSchema(",
    '            f"Invalid URL {url!r}: No scheme supplied. "',
    '            f"Perhaps you meant https://{url}?"',
    "        )",
    "",
    "    if not host:",
    '        raise InvalidURL(f"Invalid URL {url!r}: No host supplied")',
    "    self.url = url",
]
PY_EVIDENCE = [
    {
        "id": "E3",
        "file": "models.py",
        "start_line": 483,
        "end_line": 508,
        "kind": "method",
    }
]


def read_py(file, start, end):
    assert file == "models.py"
    return PY[start - 483 : end - 483 + 1]


def test_notice_terms_come_only_from_a_what_to_notice_caption():
    notice = (
        "*What to notice:* it raises `MissingSchema` or `InvalidURL` and sets "
        "`self.url` via `Session.prepare_url()`. [E3]"
    )
    assert notice_terms(notice) == [
        "MissingSchema",
        "InvalidURL",
        "self.url",
        "url",
        "Session.prepare_url",
        "prepare_url",
    ]
    assert notice_terms("Source excerpt from `prepare_url()`. [E3]") == []


def test_prose_lines_are_comments_and_docstrings_but_not_code_that_looks_like_them():
    assert prose_lines(PY[:8]) == [False, True, True, True, True, False, True, False]
    assert prose_lines(["    **kwargs: Any,", " * continues a block comment"]) == [
        False,
        True,
    ]
    assert prose_lines(["#include <stdio.h>", "#[derive(Debug)]", "# note"]) == [
        False,
        False,
        True,
    ]


def test_signature_end_counts_a_long_signature_in_full():
    lines = ["def post(", "    url,", "    data=None,", "    json=None,", "    **kw,"]
    lines += [") -> Response:", "    return request(url)"]
    assert signature_span(lines) == (0, 1)
    assert signature_end(lines) == 6


def test_a_notice_marks_the_code_it_names_and_the_window_follows_it():
    # The generator picked the comment-heavy top of the body.
    markdown = (
        "```python\n    url = url.strip()\n"
        "    # Don't prepare non-HTTP schemes such as `mailto`.\n```\n\n"
        "*What to notice:* a bad URL raises `MissingSchema` or `InvalidURL`. "
        "[E3](#evidence-E3)"
    )
    out = anchor_excerpts(markdown, PY_EVIDENCE, read_py)
    head, *body = out.split("\n```", 1)[0].split("\n")
    # The first two raises are marked; the comment between them is not.
    assert head == "```python at=483,494 hl=497,501 cite=E3"
    assert body[0] == "def prepare_url(self, url, params):"
    assert body[1] == GAP
    # Three lines of context around the marks, from `try:` to the `)`.
    assert body[2:] == PY[11:22]
    assert body[-1] == "        )"


def test_a_name_on_many_lines_marks_none_of_them():
    terms = notice_terms("*What to notice:* how `url` is checked. [E3]")
    begin, end, marks = focus_window(PY, 5, 10, terms, body=1)
    assert marks == [] and (begin, end) == (5, 10)


def test_a_bare_excerpt_skips_the_docstring_but_a_notice_may_show_it():
    lines = ["def post(url):", '    """Sends a POST.', "", "    More.", '    """']
    lines += ["    return request('post', url)"]
    assert focus_window(lines, 0, 5, [], body=1)[:2] == (5, 6)
    kept = focus_window(lines, 0, 5, [], body=1, skip_docstring=False)
    assert kept[:2] == (0, 5)


def test_brackets_balance_but_blocks_may_stay_open():
    lines = ["\tif x {", "\t\tcall(a,", "\t\t\tb)", "\t}"]
    # A brace language's block opener is not grown to its end ...
    assert focus_window(lines, 0, 1, [], body=0)[:2] == (0, 1)
    # ... but a call cut after its first argument is.
    assert focus_window(lines, 0, 2, [], body=0)[:2] == (0, 3)
    # A call that never closes nearby leaves the window as it was.
    open_call = ["run(", *[f"    arg{index}," for index in range(20)], ")"]
    assert focus_window(open_call, 0, 3, [], body=0)[:2] == (0, 3)


def test_a_window_does_not_open_on_the_end_of_the_block_above():
    lines = ["func f() {", "\tif a {", "\t\tb()", "\t\tc()", "\t}"]
    lines += ["\tx := 1", "\ty := 2", "\troot := t.root", "}"]
    terms = notice_terms("*What to notice:* it reads `t.root`. [E1]")
    # Three lines of context above the mark would start on the `}`.
    begin, end, marks = focus_window(lines, 1, 5, terms, body=1)
    assert (begin, marks) == (5, [7])


def test_anchoring_twice_changes_nothing():
    markdown = (
        "```python\n    url = url.strip()\n```\n\n"
        "*What to notice:* a bad URL raises `MissingSchema`. [E3](#evidence-E3)"
        "\n\n```python\n    url = url.strip()\n```\n\nSource excerpt. [E3]"
    )
    once = anchor_excerpts(markdown, PY_EVIDENCE, read_py)
    assert once != markdown
    assert anchor_excerpts(once, PY_EVIDENCE, read_py) == once
