# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""The repository's README and docs/ files as Wiki pages."""

from __future__ import annotations

from pathlib import Path

from codenib.wiki.repo_docs import clean_markdown, doc_page, find_docs, is_doc_id


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "README.md").write_text(
        '<p align="center"><img src="logo.png"></p>\n\n'
        "# Widget\n\nRead the [guide](docs/guide.md) and [API](docs/api/).\n"
        "![diagram](docs/img/flow.png)\n"
    )
    docs = tmp_path / "docs"
    (docs / "api").mkdir(parents=True)
    (docs / "index.md").write_text("Welcome.\n")
    (docs / "guide.md").write_text(
        "# The Guide\n\n!!! note\n    Keep it short.\n\nBack to [home](index.md),\n"
        "see [code](../widget/core.py) or [the web](https://example.com).\n"
    )
    (docs / "api" / "index.md").write_text("# API\n")
    (docs / "zeta.md").write_text("# Zeta\n")
    (docs / ".hidden").mkdir()
    (docs / ".hidden" / "x.md").write_text("# Hidden\n")
    (tmp_path / "mkdocs.yml").write_text(
        "site_name: W\nnav:\n  - Home: index.md\n  - guide.md\n"
        "markdown_extensions:\n  - pymdownx.emoji:\n"
        "      emoji_index: !!python/name:material.extensions.emoji.twemoji\n"
    )
    return tmp_path


def test_readme_first_then_mkdocs_nav_then_paths(tmp_path: Path) -> None:
    docs = find_docs(_repo(tmp_path))
    assert [(d.id, d.title, d.path) for d in docs] == [
        ("doc--readme", "Widget", "README.md"),
        ("doc--docs-index", "Home", "docs/index.md"),
        ("doc--docs-guide", "The Guide", "docs/guide.md"),
        ("doc--docs-api-index", "API", "docs/api/index.md"),
        ("doc--docs-zeta", "Zeta", "docs/zeta.md"),
    ]
    assert all(is_doc_id(d.id) for d in docs) and not is_doc_id("overview")


def test_pages_drop_html_and_images_and_link_documents(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    readme = doc_page(root, "doc--readme")
    assert readme["markdown"].startswith("# Widget")
    assert "<img" not in readme["markdown"] and "flow.png" not in readme["markdown"]
    assert "[guide](?p=doc--docs-guide)" in readme["markdown"]
    assert "[API](?p=doc--docs-api-index)" in readme["markdown"]
    assert readme["generation"] == {
        "mode": "document",
        "model": None,
        "repaired": False,
        "path": "README.md",
    }
    assert readme["citations"][0]["file"] == "README.md"

    guide = doc_page(root, "doc--docs-guide")["markdown"]
    assert "> **Note**\n> Keep it short." in guide
    assert "[home](?p=doc--docs-index)" in guide
    assert "see code or [the web](https://example.com)" in guide

    index = doc_page(root, "doc--docs-index")["markdown"]
    assert index.startswith("# Home\n\nWelcome.")
    assert doc_page(root, "doc--missing") is None


def test_no_docs_no_pages(tmp_path: Path) -> None:
    assert find_docs(tmp_path) == []


def test_code_fences_are_left_alone() -> None:
    text = "```html\n<b>[x](y.md)</b>\n```\n<b>bold</b>"
    assert (
        clean_markdown(text, "README.md", {}) == "```html\n<b>[x](y.md)</b>\n```\nbold"
    )
