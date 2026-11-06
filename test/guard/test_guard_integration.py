# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""End to end: build a commit's graph, edit the tree, and run the guard and its hook."""

from __future__ import annotations

import json
import shutil
import textwrap

import pytest

from codenib.guard import hooks
from codenib.guard.notes import add_note
from codenib.guard.report import render_text, run_guard

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("pyrefly") is None and shutil.which("scip-python") is None,
        reason="needs a Python graph provider (pyrefly or scip-python)",
    ),
]

FILES = {
    "shop/__init__.py": "",
    "shop/pricing.py": textwrap.dedent("""\
        def round_cents(value):
            return round(value, 2)


        class Cart:
            def __init__(self, items=None):
                self.items = list(items or [])

            def total(self):
                return round_cents(sum(price for _, price in self.items))
        """),
    "shop/checkout.py": textwrap.dedent("""\
        from shop.pricing import Cart


        def checkout(items):
            return {"total": Cart(items).total()}
        """),
    "tests/test_shop.py": textwrap.dedent("""\
        from shop.checkout import checkout
        from shop.pricing import Cart, round_cents


        def test_checkout_total():
            assert checkout([("a", 1), ("b", 2)])["total"] == 3


        def test_items_copied():
            items = [("a", 1)]
            cart = Cart(items)
            items.append(("b", 2))
            assert len(cart.items) == 1


        def test_round():
            assert round_cents(1.234) == 1.23
        """),
}


def test_guard_reports_constructor_change_and_hook_reports_once(
    git_repo, tmp_path, monkeypatch, write_files
):
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    git_repo.commit(FILES, "init")
    add_note(
        git_repo.root,
        "Cart copies its items, so callers can reuse their list.",
        ["shop/pricing.py:Cart.__init__"],
        kind="invariant",
    )
    git_repo.git("add", "-A")
    git_repo.git("commit", "-q", "-m", "note")
    write_files(
        git_repo.root,
        {
            "shop/pricing.py": FILES["shop/pricing.py"].replace(
                "list(items or [])", "items or []"
            )
        },
    )

    report = run_guard(git_repo.root)
    ids = [t.test_id for t in report.impact.tests]
    assert ids == [
        "tests/test_shop.py::test_items_copied",
        "tests/test_shop.py::test_checkout_total",
    ]
    assert [h.symbol for h in report.impact.tests[1].path] == [
        "tests/test_shop.py:test_checkout_total",
        "shop/checkout.py:checkout",
        "shop/pricing.py:Cart.__init__",
    ]
    assert [n.kind for n, _ in report.notes_on_change] == ["invariant"]
    assert [n.kind for n, _ in report.stale_notes] == ["invariant"]
    text = render_text(report)
    assert "test_round" not in text and "Run them:" in text

    payload = {"session_id": "s", "cwd": str(git_repo.root), "stop_hook_active": False}
    out, _ = hooks.stop("claude", payload)
    context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "tests/test_shop.py::test_items_copied" in context
    assert hooks.stop("claude", payload)[0] == ""  # the same change is reported once
