from scripts.agent_compile.lib.answer_diagnostics import (
    answer_diagnosis_from_cell,
    answer_diagnosis_rates,
    build_answer_diagnostic_fields,
    format_answer_diagnosis_mix,
)


def _span(file: str, start: int, end: int):
    return {"file": file, "start": start, "end": end}


def test_build_answer_diagnostic_fields_classifies_rank_gap():
    answer_spans = [
        _span("src/a.py", 1, 2),
        _span("src/noise1.py", 1, 2),
        _span("src/noise2.py", 1, 2),
        _span("src/noise3.py", 1, 2),
        _span("src/noise4.py", 1, 2),
        _span("src/b.py", 5, 6),
    ]
    gt_blocks = [_span("src/a.py", 1, 2), _span("src/b.py", 5, 6)]
    metrics = {"answer_blocks": {5: {"recall": 0.5}}}

    deduped, fields = build_answer_diagnostic_fields(
        answer="Locations: src/a.py:1-2, src/b.py:5-6",
        answer_spans=answer_spans,
        gt_blocks=gt_blocks,
        metrics=metrics,
        metrics_k=[1, 5, 10],
    )

    assert len(deduped) == 6
    assert fields["answer_span_count"] == 6
    assert fields["answer_diagnosis"] == "rank_gap"
    assert fields["answer_all_metrics"]["recall"] == 1.0
    assert set(fields["answer_top_spans"]) == {"1", "5", "10"}


def test_answer_diagnosis_from_cell_backfills_path_alias_gap():
    cell = {
        "answer": "Locations: target.py:10-20",
        "metrics": {"answer_blocks": {5: {"recall": 0.0}}},
        "answer_spans": [_span("target.py", 10, 20)],
        "gt_code_blocks": [_span("pkg/target.py", 10, 20)],
    }

    assert answer_diagnosis_from_cell(cell) == "path_alias_gap"


def test_answer_diagnosis_rates_format_mix():
    rates = answer_diagnosis_rates(
        [
            {"answer_diagnosis": "ok"},
            {"answer_diagnosis": "rank_gap"},
            {"format_failed": True},
        ]
    )

    assert rates["ok"] == 1 / 3
    assert rates["rank_gap"] == 1 / 3
    assert rates["format_gap"] == 1 / 3
    assert format_answer_diagnosis_mix(rates) == "ok:33%,rank:33%,fmt:33%"
