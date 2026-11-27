# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""A repo card's blurb should describe the project, not the README's chrome."""

import pytest

from codenib.repository_summary import readme_summary


def test_skips_restructuredtext_badge_directives():
    # scikit-learn's README opens with RST substitution definitions.
    text = """\
.. |GitHubActions| image:: https://github.com/scikit-learn/scikit-learn/badge.svg
   :target: https://github.com/scikit-learn/scikit-learn/actions

scikit-learn is a Python module for machine learning built on top of SciPy and
is distributed under the 3-Clause BSD license.
"""
    assert readme_summary(text).startswith("scikit-learn is a Python module")


def test_skips_release_announcement_banner():
    # gin's README leads with a release banner.
    text = """\
# Gin Web Framework

We're excited to announce the release of Gin 1.12.0! This release brings new
features and important bug fixes.

Gin is a high-performance HTTP web framework written in Go.
"""
    assert readme_summary(text).startswith("Gin is a high-performance HTTP")


def test_resolves_reference_style_links():
    # hugo credits its authors with [bep][] style references.
    text = """\
# hugo

A fast and flexible static site generator built with love by [bep][],
[spf13][] and [friends][] in Go.

[bep]: https://github.com/bep
"""
    summary = readme_summary(text)
    assert "bep" in summary
    assert "[" not in summary and "]" not in summary


def test_skips_stray_html_attributes():
    text = """\
# axios

href="https://thanks.dev/?utmsource&#x3D;axios&amp;utmmedium&#x3D;sponsorlist"

Promise based HTTP client for the browser and node.js.
"""
    assert readme_summary(text).startswith("Promise based HTTP client")


def test_skips_licence_metadata_lines():
    text = """\
# sympy

License: New BSD License (see the LICENSE file for details) covers all files
in the sympy repository unless stated otherwise.

SymPy is a Python library for symbolic mathematics and aims to become a
full-featured computer algebra system.
"""
    assert readme_summary(text).startswith("SymPy is a Python library")


@pytest.mark.parametrize(
    "description",
    [
        "Requests is a simple, yet elegant, HTTP library.",
        "uutils coreutils is a cross-platform reimplementation of the GNU coreutils.",
    ],
)
def test_keeps_descriptions_that_open_with_a_lowercase_project_name(description):
    # A project name is often lowercase; that must not read as a fragment.
    assert readme_summary(f"# project\n\n{description}\n") == description


def test_readme_summary_skips_wrapped_list_item_continuations():
    text = (
        "# Tokio\n\n"
        "A runtime for writing reliable, asynchronous, and slim applications with\n"
        "the Rust programming language. It is:\n\n"
        "* **Fast**: Tokio's zero-cost abstractions give you bare-metal\n"
        "  performance.\n\n"
        "* **Reliable**: Tokio leverages Rust's ownership, type system, and\n"
        "  concurrency model to reduce bugs and ensure thread safety.\n"
    )
    # The opening paragraph ends in a colon (it introduces the list), so no
    # candidate remains; the wrapped bullet line must not become one.
    assert readme_summary(text, limit=600) == ""

    prose = text.replace("It is:", "It is built for servers.")
    summary = readme_summary(prose, limit=600)
    assert summary.startswith("A runtime for writing reliable")
    assert "concurrency model" not in summary


def test_readme_summary_keeps_a_wrapped_line_that_opens_with_a_link():
    text = (
        "# Adapters\n\n"
        "[Docs](https://example.com/docs) | [Blog](https://example.com/blog)\n\n"
        "The project converts external benchmarks into\n"
        "[Harbor](https://example.com/harbor)'s task format so they can be run and\n"
        "shared through one harness. It was split out of the main\n"
        "[`harbor`](https://example.com/harbor) monorepo.\n\n"
        "[docs]: https://example.com/docs\n"
    )
    summary = readme_summary(text, limit=600)
    assert summary.startswith("The project converts external benchmarks into Harbor's")
    assert summary.endswith("harbor monorepo.")


def test_readme_summary_reads_a_chinese_opening_paragraph():
    # Chinese has no spaces between words. Counting words by spaces skipped
    # the opening paragraph and picked a later one only because its numbers
    # and units are spaced out.
    text = (
        "# weather_kit\n\n"
        "把气象站的原始读数整理成**可查询、可绘图**的时间序列 —— "
        "帮助研究者比较不同站点的数据。\n\n"
        "这个项目回答下面几个问题：\n\n"
        "1. 两个站点的温差有多大？\n\n"
        "### 实验\n"
        "结论：误差范围 0.2-0.5 K，采样间隔约 10 min (docs/notes.md)。\n"
    )
    assert readme_summary(text).startswith(
        "把气象站的原始读数整理成可查询、可绘图的时间序列"
    )


def test_readme_summary_skips_a_chinese_paragraph_that_introduces_a_list():
    text = "# 项目\n\n这个工具支持下面这些常见的数据格式和输出方式：\n\n- 第一种\n"
    assert readme_summary(text) == ""


def test_readme_summary_joins_wrapped_chinese_lines_without_a_space():
    text = "# 项目\n\n这个工具读取代码仓库，\n生成带行号引用的说明文档。\n"
    assert readme_summary(text) == "这个工具读取代码仓库，生成带行号引用的说明文档。"


def test_skips_an_invitation_to_the_community_chat():
    text = (
        "# SymPy\n\n"
        "We have a community chat at Gitter. Feel free to ask us anything there.\n\n"
        "SymPy is a Python library for symbolic mathematics and computer algebra.\n"
    )
    assert readme_summary(text, limit=600).startswith("SymPy is a Python library")
