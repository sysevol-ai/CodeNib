# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import json
import subprocess

import pytest

from codenib.threads.events import ThreadEvent, ThreadHeader, event_address
from codenib.threads.in_flight import OpenPull
from codenib.threads.log_store import ThreadStore
from codenib.threads.wiki_overlay import (
    PageIndex,
    WikiPage,
    build_overlay,
    load_wiki_pages,
)

REPO = "acme/widget"
SOURCE = (
    "def parse_config(path):\n"
    "    return open(path).read()\n"
    "\n"
    "\n"
    "def dump_config(config, path):\n"
    "    open(path, 'w').write(config)\n"
)


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture()
def checkout(tmp_path):
    repo = tmp_path / "checkout"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "config.py").write_text(SOURCE)
    (repo / "src" / "other.py").write_text("def other():\n    return 1\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    path = repo / "src" / "config.py"
    path.write_text(SOURCE.replace(".read()", ".read().strip()"))
    _git(repo, "commit", "-qam", "Strip the config text (#30)")
    return repo


def _page(page_id, *citations):
    return WikiPage.from_json(
        {
            "id": page_id,
            "title": page_id.title(),
            "citations": [
                {"file": f, "start_line": s, "end_line": e, "node_name": n}
                for f, s, e, n in citations
            ],
        }
    )


@pytest.fixture()
def pages():
    return [
        _page("loading", ("src/config.py", 1, 2, "parse_config")),
        _page("saving", ("src/config.py", 5, 6, "dump_config")),
        _page("misc", ("src/other.py", 1, 2, "other")),
    ]


def _thread(store, number, body, *, is_pr=False, closed=False):
    at = "2026-01-01T00:00:00Z"
    header = ThreadHeader(REPO, number, is_pr, f"thread {number}", "open", "ann", at)
    store.write_header(header)
    events = [
        ThreadEvent(event_address(REPO, number, "opened", number), number, "opened",
                    "ann", at, False, body, {}),
    ]  # fmt: skip
    if closed:
        events.append(
            ThreadEvent(event_address(REPO, number, "closed", 1), number, "closed",
                        "ann", "2026-01-02T00:00:00Z", False, "", {}),
        )  # fmt: skip
    store.append(number, events)


def _overlay(pages, checkout, **kwargs):
    kwargs.setdefault("history", 0)
    return build_overlay(
        pages, repo=REPO, checkout=checkout, now="2026-01-09T00:00:00+00:00", **kwargs
    )


def test_an_issue_lands_on_the_page_that_explains_the_code_it_names(
    pages, checkout, tmp_path
):
    store = ThreadStore(REPO, tmp_path / "store")
    _thread(store, 7, "Crash in `parse_config`, see src/config.py:1")
    overlay = _overlay(pages, checkout, store=store)

    loading = overlay["pages"]["loading"]["issues"]
    assert [(i["number"], i["strength"]) for i in loading] == [(7, "symbol")]
    assert loading[0]["url"] == "https://github.com/acme/widget/issues/7"
    assert loading[0]["cites"] == ["#7/opened/7"]
    # The same file, another symbol: the page is reached, but only by file.
    assert overlay["pages"]["saving"]["issues"][0]["strength"] == "file"
    assert "misc" not in overlay["pages"]
    assert overlay["summary"]["threads_placed"] == 1


def test_a_closed_issue_is_left_out_unless_asked_for(pages, checkout, tmp_path):
    store = ThreadStore(REPO, tmp_path / "store")
    _thread(store, 8, "See src/config.py:1", closed=True)

    assert _overlay(pages, checkout, store=store)["pages"] == {}
    overlay = _overlay(pages, checkout, store=store, include_closed=True)
    assert overlay["pages"]["loading"]["issues"][0]["state"] == "closed"


def test_an_open_pull_request_lands_on_the_page_whose_lines_it_changes(pages, checkout):
    pulls = [
        OpenPull(21, "Atomic save", "bo", "2026-01-04T00:00:00Z",
                 files={"src/config.py": [[6, 6]]}),
    ]  # fmt: skip
    overlay = _overlay(pages, checkout, open_pulls=pulls)

    saving = overlay["pages"]["saving"]["pulls"][0]
    assert (saving["number"], saving["strength"]) == (21, "symbol")
    assert saving["symbols"] == ["dump_config"]
    assert saving["url"] == "https://github.com/acme/widget/pull/21"
    assert overlay["pages"]["loading"]["pulls"][0]["strength"] == "file"


def test_an_indexed_pull_request_is_placed_once_by_its_diff(pages, checkout, tmp_path):
    store = ThreadStore(REPO, tmp_path / "store")
    _thread(store, 21, "Also touches src/other.py", is_pr=True)
    pulls = [
        OpenPull(21, "Atomic save", "bo", "2026-01-04T00:00:00Z",
                 files={"src/config.py": [[6, 6]]}),
    ]  # fmt: skip
    overlay = _overlay(pages, checkout, store=store, open_pulls=pulls)

    assert "misc" not in overlay["pages"]  # what the thread says is not used
    assert len(overlay["pages"]["saving"]["pulls"]) == 1


def test_a_wide_change_is_not_shown_on_pages_it_only_reaches_by_file(checkout):
    pages = [_page(f"p{n}", ("src/config.py", 1, 2, "parse_config")) for n in range(6)]
    pulls = [
        OpenPull(40, "Reformat", "cy", "2026-01-05T00:00:00Z",
                 files={"src/config.py": [[40, 90]]}),
        OpenPull(41, "Fix parse", "dee", "2026-01-05T00:00:00Z",
                 files={"src/config.py": [[2, 2]]}),
    ]  # fmt: skip
    overlay = _overlay(pages, checkout, open_pulls=pulls)

    for page in pages:
        assert [p["number"] for p in overlay["pages"][page.id]["pulls"]] == [41]


def test_a_recent_change_lands_on_the_page_of_the_symbol_it_changed(pages, checkout):
    overlay = _overlay(pages, checkout, history=5)

    change = overlay["pages"]["loading"]["changes"][0]
    assert (change["number"], change["strength"]) == (30, "symbol")
    assert change["symbols"] == ["parse_config"]
    assert change["url"] == "https://github.com/acme/widget/pull/30"
    assert len(change["commit"]) == 40
    assert overlay["pages"]["saving"]["changes"][0]["strength"] == "file"
    assert overlay["summary"]["changes_placed"] == 1


def test_symbol_names_match_across_qualified_forms():
    index = PageIndex([_page("p", ("src/a.rs", 100, 120, "Checker::visit_expr"))])
    # Lines moved since the Wiki was written; the name still matches.
    assert index.match(
        "src/a.rs", [(10, 12)], ["src/a.rs:impl Checker.visit_expr"]
    ) == {"p": ("symbol", ["Checker::visit_expr"])}
    assert index.match("src/a.rs", [(10, 12)], []) == {"p": ("file", [])}
    # A citation that names its symbol by chunk id is shown without the path.
    by_id = PageIndex([_page("q", ("src/a.rs", 5, 6, "src/a.rs:Foo.new()"))])
    assert by_id.match("src/a.rs", [(5, 5)], []) == {"q": ("symbol", ["Foo.new()"])}
    assert index.match("src/b.rs", [(100, 100)], []) == {}


def test_load_wiki_pages_reads_a_published_repository_in_page_order(tmp_path):
    root = tmp_path / "data" / "repos" / "acme__widget"
    (root / "pages").mkdir(parents=True)
    tree = [{"id": "overview", "children": [{"id": "loading"}]}, {"id": "saving"}]
    (root / "wiki.json").write_text(json.dumps({"repo": REPO, "pages": tree}))
    for page_id in ("saving", "loading", "overview"):
        citations = [{"file": f"/src/{page_id}.py", "start_line": 3, "end_line": None}]
        (root / "pages" / f"{page_id}.json").write_text(
            json.dumps({"id": page_id, "title": page_id, "citations": citations})
        )
    (root / "pages" / "broken.json").write_text("{")

    pages = load_wiki_pages(root)
    assert [p.id for p in pages] == ["overview", "loading", "saving"]
    assert pages[1].citations[0].file == "src/loading.py"
    assert (pages[1].citations[0].start_line, pages[1].citations[0].end_line) == (3, 3)


def test_overlay_command_writes_the_overlay(pages, checkout, tmp_path, capsys):
    from codenib.cli import run

    wiki = tmp_path / "wiki"
    (wiki / "pages").mkdir(parents=True)
    for page in pages:
        cited = [
            {"file": c.file, "start_line": c.start_line, "end_line": c.end_line,
             "node_name": c.name}
            for c in page.citations
        ]  # fmt: skip
        (wiki / "pages" / f"{page.id}.json").write_text(
            json.dumps({"id": page.id, "title": page.title, "citations": cited})
        )
    store = ThreadStore(REPO, tmp_path / "store")
    _thread(store, 7, "See src/config.py:1")
    output = tmp_path / "threads.json"

    status = run(
        ["threads", "overlay", "--wiki", str(wiki), "--output", str(output),
         "--checkout", str(checkout), "--repo", REPO,
         "--store", str(tmp_path / "store")]
    )  # fmt: skip
    assert status == 0, capsys.readouterr().err
    overlay = json.loads(output.read_text())
    assert overlay["pages"]["loading"]["issues"][0]["number"] == 7
    assert overlay["pages"]["loading"]["changes"][0]["number"] == 30

    status = run(
        ["threads", "overlay", "--wiki", str(tmp_path / "empty"),
         "--checkout", str(checkout), "--repo", REPO]
    )  # fmt: skip
    assert status != 0
    assert "no Wiki pages" in capsys.readouterr().err


def test_file_level_matches_on_a_hot_file_are_dropped(pages, checkout):
    config = checkout / "src" / "config.py"
    for n in range(6):
        config.write_text(config.read_text() + f"# note {n}\n")
        _git(checkout, "commit", "-qam", f"Note {n}")
    pulls = [
        OpenPull(21, "Atomic save", "bo", "2026-01-04T00:00:00Z",
                 files={"src/config.py": [[6, 6]]}),
    ]  # fmt: skip
    overlay = _overlay(pages, checkout, open_pulls=pulls)

    # Seven of eight commits touched config.py: reaching it says little about
    # a page, but changing the symbol a page cites still does.
    assert overlay["summary"]["hot_files"] == ["src/config.py"]
    assert overlay["pages"]["saving"]["pulls"][0]["strength"] == "symbol"
    assert "loading" not in overlay["pages"]
    assert overlay["summary"]["hot_file_matches_dropped"] == 1

    kept = _overlay(pages, checkout, open_pulls=pulls, heat_window=0)
    assert kept["pages"]["loading"]["pulls"][0]["strength"] == "file"
