# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the pull-request comment a guard report renders to."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from codenib.guard.comment import MARKER, render_comment
from codenib.guard.impact import ChangedSymbol, Hop, Impact, ImpactGraph
from codenib.guard.impact import TestAtRisk as AtRisk
from codenib.guard.report import GuardReport

BASE = "b" * 40
URL = "https://github.com/acme/shop"


def _report(**impact) -> GuardReport:
    return GuardReport("repo", "main", BASE, Impact(**impact))


def _source_change() -> dict:
    reaching = AtRisk(
        "tests/test_checkout.py::test_total",
        "tests/test_checkout.py:test_total",
        2,
        (
            Hop("tests/test_checkout.py:test_total", "tests/test_checkout.py", 4, 6),
            Hop("shop/checkout.py:checkout", "shop/checkout.py", 3, 5),
            Hop("shop/pricing.py:Cart.total", "shop/pricing.py", 9, None),
        ),
    )
    return {
        "changed": [
            ChangedSymbol(
                "shop/pricing.py:Cart.total", "shop/pricing.py", 9, "function"
            ),
            ChangedSymbol(
                "shop/pricing.py:round_cents", "shop/pricing.py", 1, "function"
            ),
            ChangedSymbol(
                "tests/test_pricing.py:test_round",
                "tests/test_pricing.py",
                3,
                "function",
            ),
        ],
        "callers": {
            "shop/pricing.py:Cart.total": [
                Hop("shop/checkout.py:checkout", "shop/checkout.py", 3, 5),
                Hop("shop/pricing.py:round_cents", "shop/pricing.py", 1, 2),
                Hop("tests/test_pricing.py:test_total", "tests/test_pricing.py", 8, 9),
            ],
            "shop/pricing.py:round_cents": [],
        },
        "tests_reaching": {
            "shop/pricing.py:Cart.total": 4,
            "shop/pricing.py:round_cents": 0,
        },
        "source_tests": [reaching],
        "total_tests": 1204,
    }


def test_comment_counts_callers_the_change_left_alone():
    text = render_comment(_report(**_source_change()), source_url=URL)

    assert text.startswith(MARKER)
    assert "changes **2 definitions** in `shop/pricing.py`" in text
    # A caller the change also edits, and a caller in a test file, are not
    # callers the reviewer still has to look at.
    assert "**1 caller** outside the change calls that code" in text
    assert "**1 of 1,204 tests** reaches it within 3 calls" in text
    assert f"| [`Cart.total`]({URL}/blob/{BASE}/shop/pricing.py#L9) | 1 | 4 |" in text
    assert (
        f"| [`round_cents`]({URL}/blob/{BASE}/shop/pricing.py#L1) | 0 | none found |"
        in text
    )
    # The caller links to the line of its call, not of its definition.
    assert f"[`checkout`]({URL}/blob/{BASE}/shop/checkout.py#L5)" in text
    assert "- `tests/test_checkout.py`: 1 test, such as " in text
    assert "`test_total` → `checkout` → `Cart.total`" in text
    assert "python -m pytest tests/test_checkout.py" in text
    assert "also edits 1 test file" in text


def test_comment_without_a_repository_address_has_no_links():
    text = render_comment(_report(**_source_change()))

    assert "| `Cart.total` | 1 | 4 |" in text
    assert "](" not in text


def test_comment_is_empty_when_only_new_tests_change():
    report = _report(
        changed=[
            ChangedSymbol(
                "tests/test_pricing.py:test_round",
                "tests/test_pricing.py",
                3,
                "function",
            )
        ]
    )

    assert render_comment(report, source_url=URL) == ""
    assert render_comment(_report(), source_url=URL) == ""


def test_comment_names_rewritten_tests_even_without_source_changes():
    report = _report(
        changed=[
            ChangedSymbol(
                "tests/test_pricing.py:test_round",
                "tests/test_pricing.py",
                3,
                "function",
            )
        ],
        edited_tests=["tests/test_pricing.py::test_round"],
    )
    text = render_comment(report)

    assert "changes no source definitions" in text
    assert "**1 existing test was rewritten by this change.**" in text
    assert "- `tests/test_pricing.py::test_round`" in text


def test_comment_disarms_markup_in_note_titles():
    note = SimpleNamespace(
        title="Ask @alice <b>first</b> | `x`", path=Path(".codenib/notes/ask.md")
    )
    report = _report(**_source_change())
    report.notes_on_change = [(note, ["shop/pricing.py:Cart.total"])]
    text = render_comment(report)

    assert "@alice" not in text
    assert "<b>" not in text
    assert "Ask @​alice \\<b\\>first\\</b\\> \\| \\`x\\`" in text


def test_impact_keeps_callers_and_source_tests_apart(make_graph, tmp_path):
    graph = make_graph(
        [
            ("shop/pricing.py", "file", None, None, None),
            ("shop/pricing.py:total()", "function", "shop/pricing.py", 0, 2),
            ("shop/checkout.py:checkout()", "function", "shop/checkout.py", 0, 3),
            ("tests/test_shop.py:helper()", "function", "tests/test_shop.py", 0, 1),
            (
                "tests/test_shop.py:test_checkout()",
                "function",
                "tests/test_shop.py",
                3,
                5,
            ),
            (
                "tests/test_shop.py:test_helper()",
                "function",
                "tests/test_shop.py",
                7,
                9,
            ),
        ],
        [
            ("shop/checkout.py:checkout()", "shop/pricing.py:total()", "reference", 2),
            (
                "tests/test_shop.py:test_checkout()",
                "shop/checkout.py:checkout()",
                "reference",
                4,
            ),
            (
                "tests/test_shop.py:test_helper()",
                "tests/test_shop.py:helper()",
                "reference",
                8,
            ),
        ],
    )
    impact = ImpactGraph(graph, tmp_path).impact(
        {"shop/pricing.py": {2}, "tests/test_shop.py": {1}}
    )

    callers = impact.callers["shop/pricing.py:total"]
    assert [(hop.symbol, hop.call_line) for hop in callers] == [
        ("shop/checkout.py:checkout", 3)
    ]
    assert impact.tests_reaching["shop/pricing.py:total"] == 1
    # test_helper reaches only a changed test helper, not changed source.
    assert [t.test_id for t in impact.source_tests] == [
        "tests/test_shop.py::test_checkout"
    ]
    assert {t.test_id for t in impact.tests} == {
        "tests/test_shop.py::test_checkout",
        "tests/test_shop.py::test_helper",
    }
