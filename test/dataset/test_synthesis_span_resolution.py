# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Regression test: hint-category synthesis must carry real line spans.

The HuggingFace ``codeminer-synthesis`` build had (0,0) ``gt_symbol_nodes`` for
the file_hint / module_hint / symbol_hint / reasoning categories, because their
ground truth is derived from ``file:symbol`` strings (no line ranges). The fix
resolves each symbol's real span from the graph-sampled ``candidate_blocks``
(built for every query). This locks that behavior at the unit level so a
re-generated dataset has usable spans for span-overlap eval.
"""

from codeminer.dataset.synthesize._types import BehavioralContext, SampledCodeBlock
from codeminer.dataset.synthesize.query_synthesizer import ClaudeQuerySynthesizer
from codeminer.dataset.utils import CodeLocation, GroundTruth


def _block(file: str, node_name: str, start: int, end: int) -> SampledCodeBlock:
    return SampledCodeBlock(
        block_id="blk_001",
        node_id=1,
        node_name=node_name,
        file_path=file,
        node_type="function",
        start_line=start,
        end_line=end,
        content="...",
        char_count=3,
        line_count=1,
    )


def _leaf(name: str) -> str:
    s = str(name)
    if ":" in s:
        s = s.split(":")[-1]
    return s.split("/")[-1].split(".")[-1].rstrip("()")


def _ctx(*blocks: SampledCodeBlock) -> BehavioralContext:
    # symbol_spans is built from the FULL graph by context_loader; here we build
    # it from the given blocks to mirror that (resolution reads symbol_spans, not
    # candidate_blocks, which is downsampled).
    spans = {
        (b.file_path, _leaf(b.node_name)): (b.start_line, b.end_line) for b in blocks
    }
    return BehavioralContext(
        core_block=blocks[0],
        candidate_blocks=list(blocks),
        neighborhood_blocks=[],
        symbol_spans=spans,
    )


def _gt(file: str, symbol: str) -> GroundTruth:
    # start/end_line 0 reproduces the spanless _build_ground_truth output.
    return GroundTruth(
        primary_locations=[
            CodeLocation(
                file_path=file,
                symbol=symbol,
                start_line=0,
                end_line=0,
                symbol_type="function",
            )
        ]
    )


def test_resolves_span_from_blocks_file_symbol_nodename():
    gt = _gt("astropy/stats/funcs.py", "binom_conf_interval")
    blk = _block(
        "astropy/stats/funcs.py",
        "astropy/stats/funcs.py:binom_conf_interval()",  # file:symbol() form
        89,
        313,
    )
    nodes = ClaudeQuerySynthesizer._build_symbol_nodes_from_ground_truth(gt, _ctx(blk))
    assert len(nodes) == 1
    assert (nodes[0].start_line, nodes[0].end_line) == (89, 313)


def test_resolves_span_from_graph_style_nodename():
    # graph node["name"] is path-like (no file: prefix, no extension)
    gt = _gt("src/m.rs", "do_it")
    blk = _block("src/m.rs", "crate/m/do_it", 10, 40)
    nodes = ClaudeQuerySynthesizer._build_symbol_nodes_from_ground_truth(gt, _ctx(blk))
    assert (nodes[0].start_line, nodes[0].end_line) == (10, 40)


def test_falls_back_to_zero_when_unmatched_or_no_context():
    gt = _gt("a.py", "foo")
    # no behavioral_context -> unchanged (0,0) fallback (no regression / no crash)
    nodes = ClaudeQuerySynthesizer._build_symbol_nodes_from_ground_truth(gt, None)
    assert (nodes[0].start_line, nodes[0].end_line) == (0, 0)
    # context present but no matching block -> still falls back
    blk = _block("other.py", "other.py:bar()", 5, 9)
    nodes = ClaudeQuerySynthesizer._build_symbol_nodes_from_ground_truth(gt, _ctx(blk))
    assert (nodes[0].start_line, nodes[0].end_line) == (0, 0)
