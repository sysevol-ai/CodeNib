# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the synthesis query-quality cleanup fixes.

These lock in the pure-function behaviours behind the v2 dataset quality pass:

* ``_strip_leading_narration`` / ``_coerce_question_text`` — strip agent
  chain-of-thought ("Now let me look at…") that leaked into the query field and
  produced the malformed ``:?`` the judge rejected.
* ``_modules_from_ground_truth`` / ``_is_source_path`` — derive the dotted
  module for MODULE_HINT and drop non-source anchors (bundles, ``.d.cts``).
* ``post_fix`` anchor-content recovery — the mixed rows store ``content=null``,
  so the post-fix regenerate path must recover code from the behavioral
  anchors; without it every flagged mixed row was skipped, not regenerated.
"""

from codeminer.dataset.synthesize.query_curator import QueryCurator
from codeminer.utils import is_non_source_file
from scripts._post_fix import _build_anchor_content_lookups, _get_anchor

# ---------------------------------------------------------------------------
# Narration stripping (B)
# ---------------------------------------------------------------------------


def test_strip_leading_narration_drops_pure_thinking():
    q = "Now let me look at how `TimeBase.insert` interacts with callers:"
    assert QueryCurator._strip_leading_narration(q) == ""


def test_strip_leading_narration_keeps_real_query_after_narration():
    q = "Let me look at this. How does the fast C reader validate delimiters?"
    assert (
        QueryCurator._strip_leading_narration(q)
        == "How does the fast C reader validate delimiters?"
    )


def test_strip_leading_narration_leaves_clean_query_untouched():
    q = "Why does comoving distance fail near zero redshift?"
    assert QueryCurator._strip_leading_narration(q) == q


def test_coerce_question_text_skips_narration_only_blob():
    blob = "Now I understand the code.\nLet me check the callers next:"
    assert QueryCurator._coerce_question_text(blob) == ""


def test_coerce_question_text_recovers_real_question_line():
    blob = "Let me explore.\nHow does the loader resolve nested imports?"
    assert (
        QueryCurator._coerce_question_text(blob)
        == "How does the loader resolve nested imports?"
    )


# ---------------------------------------------------------------------------
# Module derivation + non-source filtering (C, #4)
# ---------------------------------------------------------------------------


def test_modules_from_ground_truth_derives_dotted_module():
    gt = {"target_files": ["astropy/units/core.py"]}
    assert QueryCurator._modules_from_ground_truth(gt) == ["astropy.units.core"]


def test_modules_from_ground_truth_drops_non_source():
    gt = {
        "target_files": [
            "src/core/dispatchRequest.js",
            "dist/esm/axios.js",
            "index.d.cts",
            "lib/foo.min.js",
        ]
    }
    # Only the real source file survives; bundle / type-def / minified drop out.
    assert QueryCurator._modules_from_ground_truth(gt) == ["src.core.dispatchRequest"]


def test_is_source_path_rejects_build_artifacts():
    assert not QueryCurator._is_source_path("dist/esm/axios.js")
    assert not QueryCurator._is_source_path("index.d.cts")
    assert not QueryCurator._is_source_path("build/out/x.js")
    assert not QueryCurator._is_source_path("node_modules/dep/index.js")
    assert QueryCurator._is_source_path("src/core/dispatchRequest.js")
    assert QueryCurator._is_source_path("astropy/units/core.py")


def test_is_non_source_file_blocks_synthesis_anchors():
    # Used by behavioral candidate sampling + traversal seed selection so
    # bundles / type-def stubs never become ground truth.
    assert is_non_source_file("dist/esm/axios.js")
    assert is_non_source_file("index.d.cts")
    assert is_non_source_file("src/jsx.d.ts")
    assert is_non_source_file("a/b.min.js")
    assert is_non_source_file("dist/bundle.js:Foo")  # node-id form (file:symbol)
    assert not is_non_source_file("lib/core/AxiosHeaders.js")
    assert not is_non_source_file("src/cat/main.rs")
    assert not is_non_source_file("astropy/time/core.py:TimeBase.insert()")


# ---------------------------------------------------------------------------
# post_fix anchor-content recovery (F)
# ---------------------------------------------------------------------------


def test_post_fix_recovers_anchor_content_from_anchors():
    # Mixed row as produced by multiply_queries: gt_symbol_nodes carry the
    # target identity but content=null.
    row = {
        "gt_files": ["astropy/time/core.py"],
        "gt_symbols": ["astropy/time/core.py:TimeBase.insert()"],
        "gt_symbol_nodes": [
            {
                "node_name": "astropy/time/core.py:TimeBase.insert()",
                "content": None,
                "file": "astropy/time/core.py",
            }
        ],
    }
    anchors = [
        {
            "anchor_symbol": "astropy/time/core.py:TimeBase.insert()",
            "anchor_file": "astropy/time/core.py",
            "anchor_content": "class TimeBase:\n    def insert(self, idx): ...",
            "target_files": ["astropy/time/core.py"],
        }
    ]
    by_node, by_file = _build_anchor_content_lookups([row], anchors)
    _, _, content = _get_anchor(row, by_node, by_file)
    assert content  # non-empty -> regenerate runs instead of being skipped
    assert "def insert" in content


def test_post_fix_without_anchors_finds_no_content_for_null_rows():
    # Regression guard: the bug was that mixed rows alone yield no content.
    row = {
        "gt_files": ["astropy/time/core.py"],
        "gt_symbols": ["astropy/time/core.py:TimeBase.insert()"],
        "gt_symbol_nodes": [
            {
                "node_name": "astropy/time/core.py:TimeBase.insert()",
                "content": None,
                "file": "astropy/time/core.py",
            }
        ],
    }
    by_node, by_file = _build_anchor_content_lookups([row])
    _, _, content = _get_anchor(row, by_node, by_file)
    assert content == ""
