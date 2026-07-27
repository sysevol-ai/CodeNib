# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Fast unit tests for the index-derived wiki builder (no real indexes)."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from codenib.repository_summary import readme_summary
from codenib.wiki.builder import (
    WikiBuilder,
    _is_supporting_area,
    _module_label,
    _slug,
    _top_module,
)


@dataclass
class _Doc:
    page_content: str
    metadata: dict


def _make_bundle(repo_dir: str):
    """A fake RepoBundle with two synthetic classes' methods + a function."""
    docs = [
        _Doc(
            "def __call__(self):\n    return 1\n",
            {
                "file": f"{repo_dir}/pkg/mod/a.py",
                "name": "Model.__call__",
                "chunk_type": "method",
                "start_line": 10,
                "end_line": 20,
            },
        ),
        _Doc(
            "def fit(self):\n    pass\n",
            {
                "file": f"{repo_dir}/pkg/mod/a.py",
                "name": "Model.fit",
                "chunk_type": "method",
                "start_line": 22,
                "end_line": 30,
            },
        ),
        _Doc(
            "def helper():\n    pass\n",
            {
                "file": f"{repo_dir}/pkg/mod/b.py",
                "name": "helper",
                "chunk_type": "function",
                "start_line": 1,
                "end_line": 5,
            },
        ),
        _Doc(
            "def other():\n    pass\n",
            {
                "file": f"{repo_dir}/pkg/util/c.py",
                "name": "Other.run",
                "chunk_type": "method",
                "start_line": 1,
                "end_line": 9,
            },
        ),
    ]
    vs = SimpleNamespace(l2_documents=docs, l0_documents=[1, 2, 3])
    entry = SimpleNamespace(
        instance_id="x__y-1",
        repo="o/r",
        commit_short="abc123",
        language="Python",
        problem_statement="Some bug happened.",
        repo_dir=repo_dir,
    )
    return SimpleNamespace(entry=entry, vector_store=vs, bm25=None, manifest=None)


@pytest.fixture
def repo_dir(tmp_path):
    for rel in ("pkg/mod/a.py", "pkg/mod/b.py", "pkg/util/c.py"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("line1\nline2\nline3\nline4\nline5\n")
    return str(tmp_path)


def test_readme_summary_prefers_tagline():
    md = (
        "# My Project\n\n"
        "[![build](badge.svg)](ci)\n\n"
        "## Installation\n\n"
        "To install, run pip install myproject from PyPI today.\n\n"
        "My Project is a fast, friendly library for doing useful things.\n"
    )
    out = readme_summary(md)
    assert "fast, friendly library" in out
    assert "install" not in out.lower()


def test_readme_summary_skips_labels_and_short_lines():
    assert readme_summary("Requirements:\nGo 1.21\n") == ""  # label + short line


def test_top_module_depth_two_and_slug():
    assert _top_module("pkg/mod/a.py") == "pkg/mod"
    assert _top_module("pkg/a.py") == "pkg"
    assert _top_module("solo.py") == "root"
    assert _module_label("root") == "Repository root"
    assert _is_supporting_area("pkg/eval")
    assert not _is_supporting_area("pkg/runtime")
    assert _slug("astropy/IO Fits") == "astropy-io-fits"


def test_page_tree_has_overview_and_modules(repo_dir):
    wb = WikiBuilder(_make_bundle(repo_dir))
    tree = wb.page_tree()
    ids = {p["id"] for p in tree}
    assert "overview" in ids and "architecture" in ids
    arch = next(p for p in tree if p["id"] == "architecture")
    child_titles = {c["title"] for c in arch["children"]}
    assert "pkg/mod" in child_titles


def test_module_page_surfaces_classes_and_links(repo_dir):
    wb = WikiBuilder(_make_bundle(repo_dir))
    page = wb.page("mod__pkg-mod")
    assert page is not None
    md = page["markdown"]
    assert "### `Model`" in md  # synthesized class (W11)
    assert "```python" in md  # real fenced code (C3/C4)
    assert "?p=mod__" in md  # cross-page link (C9)
    assert page["diagram"].startswith("graph TD")
    assert len(page["citations"]) >= 1
    c = page["citations"][0]
    assert c["start_line"] >= 1  # 0-based -> 1-based


def test_symbol_content_is_hydrated_from_source(repo_dir):
    source = (
        "# module docs\n"
        "\n"
        "def run():\n"
        '    """Return the runtime status."""\n'
        '    return "ready"\n'
    )
    with open(f"{repo_dir}/pkg/mod/runtime.py", "w", encoding="utf-8") as handle:
        handle.write(source)
    document = _Doc(
        "pkg mod runtime py run normalized retrieval tokens",
        {
            "file": f"{repo_dir}/pkg/mod/runtime.py",
            "name": "run",
            "chunk_type": "function",
            "start_line": 2,
            "end_line": 4,
        },
    )
    bundle = _make_bundle(repo_dir)
    bundle.vector_store = None
    bundle.bm25 = SimpleNamespace(documents=[document])

    builder = WikiBuilder(bundle)
    symbol = builder._symbols()[0]
    page = builder.page("mod__pkg-mod")

    assert symbol.content == (
        "def run():\n" '    """Return the runtime status."""\n' '    return "ready"'
    )
    assert "normalized retrieval tokens" not in page["markdown"]
    assert "def run():" in page["citations"][0]["content"]


def test_source_traversal_guard(repo_dir):
    wb = WikiBuilder(_make_bundle(repo_dir))
    assert wb.source("../../../etc/passwd") is None
    ok = wb.source("pkg/mod/a.py", 1, 3)
    assert ok is not None and ok["content"].count("\n") == 3


def test_overview_links_modules(repo_dir):
    wb = WikiBuilder(_make_bundle(repo_dir))
    page = wb.page("overview")
    assert "?p=mod__" in page["markdown"]
    assert page["diagram"].startswith("graph TD")
    assert "Repository at a glance" in page["markdown"]
    assert "Representative definitions" in page["markdown"]
    assert "```mermaid" not in page["markdown"]


def test_overview_uses_indexed_source_file_count(repo_dir):
    bundle = _make_bundle(repo_dir)
    bundle.manifest = SimpleNamespace(
        languages=["python"],
        file_count=99,
        indexes={
            "bm25": SimpleNamespace(metadata={"source_file_count": 3}),
        },
    )

    markdown = WikiBuilder(bundle).page("overview")["markdown"]

    assert "| python | 3 | 4 |" in markdown
    assert "| python | 99 |" not in markdown


def test_overview_uses_readme_purpose_without_a_model(repo_dir):
    with open(f"{repo_dir}/README.md", "w", encoding="utf-8") as handle:
        handle.write(
            "# ProjectX\n\n"
            "ProjectX coordinates indexed source evidence for repository tools.\n"
        )

    markdown = WikiBuilder(_make_bundle(repo_dir)).page("overview")["markdown"]

    assert "ProjectX coordinates indexed source evidence" in markdown
    assert "generated from" not in markdown.lower()


def test_architecture_reports_area_facts_and_source_citations(repo_dir):
    page = WikiBuilder(_make_bundle(repo_dir)).page("architecture")
    markdown = page["markdown"]

    assert "4 of 4 indexed named definitions" in markdown
    assert "## Module inventory" in markdown
    assert "| [**pkg/mod**](?p=mod__pkg-mod) | 2 | 1 | 3 | 3 |" in markdown
    assert "## Key definitions" in markdown
    assert "| Definition | Kind | Area | Source |" in markdown
    assert "`Model.fit`" in markdown
    assert "```mermaid" not in markdown
    assert page["citations"]
    assert {citation["file"] for citation in page["citations"]} >= {
        "pkg/mod/a.py",
        "pkg/util/c.py",
    }


# -- Critique #8: LLM-authored content layer ---------------------------------

from codenib.llm.litellm_chat import _no_thinking_kwargs  # noqa: E402
from codenib.wiki.narrator import Narrator  # noqa: E402


def test_shared_litellm_adapter_disables_gemini_25_thinking():
    assert _no_thinking_kwargs("vertex_ai/gemini-2.5-flash") == {
        "thinking": {"type": "disabled", "budget_tokens": 0}
    }


def test_narrator_disabled_returns_none():
    """G6: with no creds the narrator yields None for every prose builder so the
    builder falls back to templated text (no crash)."""
    n = Narrator(enabled=False)
    assert n.overview("r", "py", [("m", 1)], ["X"], "k") is None
    assert n.module_intro("r", "m", ["a.py"], ["X"], "k") is None
    assert n.components("r", "m", [("X", "doc")], "k") is None


def test_overview_has_no_swebench_artifact(repo_dir):
    """G3: the wiki never shows the SWE-bench problem_statement / Example issue,
    even though the entry carries one."""
    wb = WikiBuilder(_make_bundle(repo_dir))  # default narrator is disabled
    md = wb.page("overview")["markdown"]
    assert "Example issue" not in md
    assert "Some bug happened" not in md


def test_fallback_overview_keeps_factual_lead(repo_dir):
    """G6: disabled narrator -> a deterministic repository fact is used."""
    md = WikiBuilder(_make_bundle(repo_dir)).page("overview")["markdown"]
    assert "`o/r` is a Python repository." in md
    assert "generated from" not in md.lower()


class _FakeNarrator:
    """Stub standing in for a real LLM (no network)."""

    def overview(self, repo, language, modules, highlights, key):
        return "ProjectX is a library for doing X. It is organized into modules."

    def module_intro(self, repo, module, files, components, key):
        return "This module handles the core responsibility of the package."

    def components(self, repo, module, items, key):
        return {name: f"{name} does an important job." for name, _ in items}


def test_narrative_leads_and_preserves_facts(repo_dir):
    """G1/G2/G4: narrative prose leads; structural facts + real citations stay."""
    wb = WikiBuilder(_make_bundle(repo_dir), narrator=_FakeNarrator())
    ov = wb.page("overview")["markdown"]
    # G1: narrative is the lead; meta sentence is gone.
    assert ov.lstrip().split("\n\n")[1].startswith("ProjectX is a library")
    assert "generated from" not in ov.lower()
    assert "?p=mod__" in ov  # cross-links kept

    mod = wb.page("mod__pkg-mod")
    md = mod["markdown"]
    # G4: module narrative leads; G2: component description (not "Type in ...").
    assert "This module handles the core responsibility" in md
    assert "Model does an important job." in md
    assert "### `Model`" in md  # real component heading kept
    assert "```python" in md  # real code kept
    assert len(mod["citations"]) >= 1  # real spans kept
