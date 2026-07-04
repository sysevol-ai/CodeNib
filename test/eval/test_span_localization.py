# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for overlap-based line-span localization scoring.

These cover the failure mode that motivated the metric: the agent genuinely
locates the code but the symbol-name string match scores it zero. Span overlap
must credit it, must survive the 0-based-node vs 1-based-GT origin skew, and must
not let a verbose agent inflate its score by dumping context.
"""

import pickle
from types import SimpleNamespace

import igraph as ig

from codeminer.eval.agent_runner.symbols import build_prebuilt_symbol_span_index
from codeminer.eval.retrieval_eval import (
    collect_target_blocks,
    compute_block_metrics,
    dedup_spans,
    nodes_to_spans,
    parse_answer_spans,
    resolve_symbol_spans,
    score_localization_spans,
    spans_overlap,
)


def _node(file, start, end, score=0.0, node_id=None):
    """Duck-typed QueriedNode. Retrieval nodes are ~1-based and carry a
    repo-relative ``node_id`` (``node.file`` is an absolute build-time path)."""
    return SimpleNamespace(
        file=file,
        start_line=start,
        end_line=end,
        score=score,
        node_id=node_id if node_id is not None else f"{file}:sym()",
    )


def _span(file, start, end, **kw):
    return {"file": file, "start": start, "end": end, "score": kw.get("score", 0.0)}


# --- ground-truth blocks ----------------------------------------------------


def test_collect_target_blocks_from_gt_code_blocks():
    inst = {
        "gt_code_blocks": [
            {
                "change_type": "modified",
                "start_line": 67,
                "end_line": 105,
                "file_path": "src/a.rs",
                "symbol": "foo()",
                "symbol_type": "function",
            }
        ]
    }
    blocks = collect_target_blocks(inst)
    assert blocks == [
        {
            "file": "src/a.rs",
            "start": 67,
            "end": 105,
            "score": 0.0,
            "source": "gt",
            "symbol": "foo()",
            "change_type": "modified",
        }
    ]


def test_collect_target_blocks_empty():
    assert collect_target_blocks({}) == []


def test_collect_target_blocks_from_synthesis_symbol_nodes():
    """codeminer-synthesis exposes 0-based gt_symbol_nodes (graph attrs) -> +1."""
    inst = {
        "gt_symbol_nodes": [
            {
                "file": "astropy/stats/funcs.py",
                "start_line": 89,  # 0-based (graph) -> 90 expected
                "end_line": 313,
                "node_name": "astropy/stats/funcs.py:binom_conf_interval()",
                "type": "function",
            }
        ]
    }
    blocks = collect_target_blocks(inst)
    assert len(blocks) == 1
    assert (blocks[0]["start"], blocks[0]["end"]) == (90, 314)
    assert blocks[0]["symbol"] == "astropy/stats/funcs.py:binom_conf_interval()"


def test_gt_code_blocks_take_priority_over_symbol_nodes():
    inst = {
        "gt_code_blocks": [
            {"file_path": "a.py", "start_line": 10, "end_line": 20}  # 1-based
        ],
        "gt_symbol_nodes": [{"file": "b.py", "start_line": 1, "end_line": 5}],
    }
    blocks = collect_target_blocks(inst)
    assert [(b["file"], b["start"], b["end"]) for b in blocks] == [("a.py", 10, 20)]


# --- overlap + origin skew --------------------------------------------------


def test_spans_overlap_same_file():
    assert spans_overlap(_span("a.py", 10, 20), _span("a.py", 20, 30))  # touch
    assert spans_overlap(_span("a.py", 10, 20), _span("a.py", 5, 12))
    assert not spans_overlap(_span("a.py", 10, 20), _span("a.py", 21, 30))
    assert not spans_overlap(_span("a.py", 10, 20), _span("b.py", 10, 20))


def test_retrieval_node_0based_shift_and_overlap():
    """Raw retrieval nodes are internal 0-based; scoring shifts them +1."""
    gt = collect_target_blocks(
        {
            "gt_code_blocks": [
                {"start_line": 67, "end_line": 105, "file_path": "src/fixes.rs"}
            ]
        }
    )
    spans = nodes_to_spans([_node("src/fixes.rs", 66, 104)])
    assert spans[0]["start"] == 67 and spans[0]["end"] == 105
    m = compute_block_metrics(spans, gt)
    assert m["accuracy"] == 1.0 and m["recall"] == 1.0


def test_retrieval_node_single_line_off_by_one_hits():
    """A one-line node at internal line 9 must hit 1-based GT line 10."""
    gt = collect_target_blocks(
        {"gt_code_blocks": [{"start_line": 10, "end_line": 10, "file_path": "a.py"}]}
    )
    spans = nodes_to_spans([_node("a.py", 9, 9)])
    assert (spans[0]["start"], spans[0]["end"]) == (10, 10)
    assert compute_block_metrics(spans, gt)["recall"] == 1.0


def test_nodes_to_spans_uses_relative_node_id_not_absolute_file():
    """node.file is the absolute build-time path; node_id is repo-relative."""
    n = _node("/abs/build/x.go", 10, 20, node_id="pkg/x.go:foo()")
    spans = nodes_to_spans([n])
    assert spans[0]["file"] == "pkg/x.go"


def test_named_but_missed_now_hits_by_span():
    """Symbol string match would score 0; span overlap credits the real hit."""
    gt = collect_target_blocks(
        {"gt_code_blocks": [{"start_line": 40, "end_line": 60, "file_path": "x.go"}]}
    )
    # retrieval returned the right region even with no exact symbol string match
    retrieval = nodes_to_spans([_node("x.go", 41, 59, score=0.9)])
    m = score_localization_spans(
        answer_spans=[], retrieval_spans=retrieval, gt_blocks=gt, ks=[1, 5]
    )
    assert m["retrieval_blocks"][5]["accuracy"] == 1.0
    assert m["answer_blocks"][5]["accuracy"] == 0.0  # never committed


def test_answer_can_exceed_retrieval_no_superset():
    """answer is NOT a subset of retrieval: the agent can commit to a region
    grep/reasoning found that the retriever never returned (the real axios case).
    Locks the "neither scope bounds the other" semantics."""
    gt = collect_target_blocks(
        {"gt_code_blocks": [{"start_line": 13, "end_line": 19, "file_path": "h.js"}]}
    )
    answer = parse_answer_spans("Locations: h.js:13-19")  # found via grep
    retrieval = nodes_to_spans([_node("h.js", 82, 85, score=0.9)])  # wrong region
    m = score_localization_spans(
        answer_spans=answer, retrieval_spans=retrieval, gt_blocks=gt, ks=[10]
    )
    assert m["answer_blocks"][10]["recall"] == 1.0
    assert m["retrieval_blocks"][10]["recall"] == 0.0  # answer > retrieval


# --- answer Locations parsing -----------------------------------------------


def test_parse_answer_spans_formats():
    answer = (
        "blah blah\n"
        "Files: src/a.py\n"
        "Locations: src/a.py:10-25, pkg/b.go:7, src/c.ts#L3-L9\n"
    )
    spans = parse_answer_spans(answer)
    got = {(s["file"], s["start"], s["end"]) for s in spans}
    assert got == {("src/a.py", 10, 25), ("pkg/b.go", 7, 7), ("src/c.ts", 3, 9)}


def test_parse_answer_spans_none_without_locations_line():
    # Prose line mentions must NOT be scraped (would reward verbosity).
    assert parse_answer_spans("The bug is on line 76 of src/a.py somewhere") == []


def test_parse_answer_spans_tolerates_markdown_bold():
    """Agents routinely bold the contract: ``**Locations:** ...`` must parse."""
    answer = "analysis...\n**Locations:** src/a.py:66-105\n"
    spans = parse_answer_spans(answer)
    assert spans and (spans[0]["start"], spans[0]["end"]) == (66, 105)


def test_parse_answer_spans_markdown_list_and_heading():
    for line in ("- Locations: a.py:10-20", "### Locations: a.py:10-20"):
        spans = parse_answer_spans(line)
        assert spans and (spans[0]["start"], spans[0]["end"]) == (10, 20)


# --- symbol -> span resolution (named-symbol committed credit) ---------------


def test_resolve_symbol_spans_bridges_named_symbol():
    """Agent names a symbol but gives no line range — resolved via the index."""
    index = {("src/fixes.rs", "remove_unused"): (66, 105)}
    answer = "Symbols: src/fixes.rs:remove_unused\n"
    spans = resolve_symbol_spans(answer, index)
    assert spans == [
        {
            "file": "src/fixes.rs",
            "start": 66,
            "end": 105,
            "score": 0.0,
            "source": "answer_symbol",
        }
    ]


def test_resolve_symbol_spans_strips_parens_and_qualifier():
    index = {("a.go", "method"): (10, 20)}
    # GT-style qualified + parenthesized name resolves on the bare leaf.
    spans = resolve_symbol_spans("Symbols: a.go:Receiver.method()", index)
    assert spans and (spans[0]["start"], spans[0]["end"]) == (10, 20)


def test_resolve_symbol_spans_unknown_symbol_is_dropped():
    assert resolve_symbol_spans("Symbols: a.py:does_not_exist", {}) == []


def test_prebuilt_symbol_span_index_normalizes_graph_names(tmp_path):
    inst = tmp_path / "iid"
    inst.mkdir()
    graph = ig.Graph()
    graph.add_vertices(2)
    graph.vs[0]["file"] = "src/foo.py"
    graph.vs[0]["start_line"] = 9
    graph.vs[0]["end_line"] = 19
    graph.vs[0]["name"] = "src/foo.py:bar()"
    graph.vs[0]["unified_name"] = "src/foo.py:Foo.bar()"
    graph.vs[1]["file"] = "pkg/calc.go"
    graph.vs[1]["start_line"] = 3
    graph.vs[1]["end_line"] = 7
    graph.vs[1]["name"] = "pkg/Calculator#Add"
    with (inst / "graph.pkl").open("wb") as f:
        pickle.dump({"graph": graph}, f)

    index = build_prebuilt_symbol_span_index(str(tmp_path), "iid")
    assert index[("src/foo.py", "bar")] == (10, 20)
    assert index[("pkg/calc.go", "Add")] == (4, 8)


# --- ranking, dedup, anti-flooding ------------------------------------------


def test_nodes_to_spans_ranked_by_score_desc():
    spans = nodes_to_spans(
        [_node("a.py", 1, 2, score=0.1), _node("b.py", 3, 4, score=0.9)]
    )
    assert [s["file"] for s in spans] == ["b.py", "a.py"]


def test_nodes_to_spans_can_preserve_retrieval_order():
    spans = nodes_to_spans(
        [_node("a.py", 1, 2, score=0.1), _node("b.py", 3, 4, score=0.9)],
        sort_by_score=False,
    )
    assert [s["file"] for s in spans] == ["a.py", "b.py"]


def test_dedup_collapses_overlapping_region():
    spans = [_span("a.py", 10, 20), _span("a.py", 12, 18), _span("b.py", 1, 2)]
    kept = dedup_spans(spans)
    assert len(kept) == 2  # the 12-18 duplicate of 10-20 is dropped


def test_flooding_does_not_inflate_answer_precision():
    """Dumping many wrong locations tanks precision; one right hit, padded."""
    gt = collect_target_blocks(
        {"gt_code_blocks": [{"start_line": 100, "end_line": 120, "file_path": "a.py"}]}
    )
    answer = [
        _span("a.py", 100, 120),  # the real hit
        _span("z.py", 1, 5),
        _span("z.py", 10, 15),
        _span("z.py", 20, 25),
        _span("z.py", 30, 35),
    ]
    m = score_localization_spans(
        answer_spans=answer, retrieval_spans=[], gt_blocks=gt, ks=[1, 5]
    )
    # recall/accuracy still 1.0 (found it), but precision punishes the padding.
    assert m["answer_blocks"][5]["accuracy"] == 1.0
    assert m["answer_blocks"][5]["precision"] == 1 / 5
    assert m["answer_blocks"][1]["precision"] == 1.0  # top-1 is the hit


def test_compute_block_metrics_no_gt_is_zero():
    m = compute_block_metrics([_span("a.py", 1, 2)], [])
    assert m == {"accuracy": 0.0, "precision": 0.0, "recall": 0.0, "hits": 0}


def test_partial_recall_multiple_gt_blocks():
    gt = collect_target_blocks(
        {
            "gt_code_blocks": [
                {"start_line": 10, "end_line": 20, "file_path": "a.py"},
                {"start_line": 50, "end_line": 60, "file_path": "b.py"},
            ]
        }
    )
    preds = [_span("a.py", 11, 19)]  # hits only the first
    m = compute_block_metrics(preds, gt)
    assert m["recall"] == 0.5 and m["accuracy"] == 0.0 and m["hits"] == 1
