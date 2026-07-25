# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Fast unit tests for the index-derived wiki builder (no real indexes)."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from codenib.web.repo_registry import _readme_summary
from codenib.wiki.builder import WikiBuilder, _slug, _top_module


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
    out = _readme_summary(md)
    assert "fast, friendly library" in out
    assert "install" not in out.lower()


def test_readme_summary_skips_labels_and_short_lines():
    assert _readme_summary("Requirements:\nGo 1.21\n") == ""  # label + short line


def test_top_module_depth_two_and_slug():
    assert _top_module("pkg/mod/a.py") == "pkg/mod"
    assert _top_module("solo.py") == "solo.py"  # top-level file: itself
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


# -- Critique #8: LLM-authored content layer ---------------------------------

from codenib.wiki.narrator import Narrator, _no_thinking_kwargs  # noqa: E402


def test_narrator_gemini_25_uses_litellm_thinking_zero_budget():
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


def test_fallback_overview_keeps_templated_lead(repo_dir):
    """G6: disabled narrator -> the templated factual lead is used."""
    md = WikiBuilder(_make_bundle(repo_dir)).page("overview")["markdown"]
    assert "generated from" in md.lower()


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
