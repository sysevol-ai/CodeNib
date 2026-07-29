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
