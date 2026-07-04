import json

from scripts.agent_compile import aggregate as agg


def _cell(
    instance: str,
    arm: str,
    *,
    rec: float,
    tokens: int = 1000,
    format_failed: bool = False,
    diagnosis: str = "ok",
    tool_calls=None,
):
    return {
        "subset_id": arm,
        "instance_id": instance,
        "success": True,
        "format_failed": format_failed,
        "answer_diagnosis": diagnosis,
        "metrics": {
            "files": {5: {"accuracy": rec, "recall": rec}},
            "answer_blocks": {5: {"accuracy": rec, "recall": rec}},
        },
        "total_tokens": tokens,
        "total_turns": 3,
        "tool_calls": tool_calls or [],
    }


def test_aggregate_reports_format_and_answer_diagnosis_mix():
    cells = [
        _cell("i1", "grep_only", rec=0.5, diagnosis="rank_gap"),
        _cell(
            "i2",
            "grep_only",
            rec=0.0,
            format_failed=True,
            diagnosis="format_gap",
        ),
        _cell("i1", "preinj_eager_compact", rec=1.0, diagnosis="ok"),
    ]

    result = agg.aggregate(cells, ks=[5], max_turns=10)
    grep = result["arms"]["grep_only"]
    compact = result["arms"]["preinj_eager_compact"]
    report = agg.render_markdown(result, agg.pareto_front(result), [5])

    assert grep["format_failed_rate"] == 0.5
    assert grep["answer_diagnosis_rate"]["rank_gap"] == 0.5
    assert grep["answer_diagnosis_rate"]["format_gap"] == 0.5
    assert compact["answer_diagnosis_rate"]["ok"] == 1.0
    assert (
        "| arm | n | tokens | turns | cost$ | cap% | fmt_fail | diag | files@5 |"
        in report
    )
    assert (
        "| grep_only | 2 | 1000 | 3.0 | n/a | 0% | 50% | rank:50%,fmt:50% |" in report
    )
    assert (
        "| preinj_eager_compact | 1 | 1000 | 3.0 | n/a | 0% | 0% | ok:100% |" in report
    )


def test_aggregate_reports_tool_envelope_health():
    cells = [
        _cell(
            "i1",
            "grep_only",
            rec=1.0,
            tool_calls=[
                {
                    "skill_id": "read",
                    "error": True,
                    "envelope": {
                        "status": "ok",
                        "result_type": "text",
                        "result_chars": 300,
                    },
                },
                {
                    "skill_id": "bash",
                    "error": False,
                    "envelope": {
                        "status": "error",
                        "result_type": "error",
                        "result_chars": 2000,
                        "truncated": True,
                        "error_kind": "timeout",
                    },
                },
            ],
        ),
        _cell(
            "i2",
            "grep_only",
            rec=0.0,
            tool_calls=[
                {
                    "skill_id": "grep",
                    "error": True,
                }
            ],
        ),
    ]

    result = agg.aggregate(cells, ks=[5], max_turns=10)
    stats = result["arms"]["grep_only"]["tool_envelope"]
    report = agg.render_markdown(result, agg.pareto_front(result), [5])

    assert stats["tool_call_count"] == 3.0
    assert stats["tool_envelope_coverage"] == 2 / 3
    assert stats["tool_error_count"] == 2.0
    assert stats["tool_truncated_count"] == 1.0
    assert stats["tool_result_chars"] == 2300.0
    assert "| grep_only | 3 | 67% | 2 | 0 | 1 | 2.3 |" in report


def test_aggregate_requires_tool_envelope_health(tmp_path, capsys):
    cells_dir = tmp_path / "cells"
    output_dir = tmp_path / "out"
    cells_dir.mkdir()
    (cells_dir / "cell.json").write_text(
        json.dumps(
            _cell(
                "i1",
                "grep_only",
                rec=1.0,
                tool_calls=[{"skill_id": "read", "error": False}],
            )
        ),
        encoding="utf-8",
    )

    rc = agg.main(
        [
            "--cells-dir",
            str(cells_dir),
            "--output-dir",
            str(output_dir),
            "--require-tool-envelope-health",
        ]
    )
    captured = capsys.readouterr()
    metrics = json.loads((output_dir / "metrics.json").read_text())

    assert rc == 2
    assert "tool envelope health gate failed" in captured.err
    assert "grep_only:incomplete_tool_envelopes" in captured.err
    assert (
        metrics["aggregate"]["arms"]["grep_only"]["tool_envelope"][
            "tool_envelope_coverage"
        ]
        == 0.0
    )


def test_aggregate_tool_envelope_health_gate_passes(tmp_path, capsys):
    cells_dir = tmp_path / "cells"
    output_dir = tmp_path / "out"
    cells_dir.mkdir()
    (cells_dir / "cell.json").write_text(
        json.dumps(
            _cell(
                "i1",
                "grep_only",
                rec=1.0,
                tool_calls=[
                    {
                        "skill_id": "read",
                        "error": True,
                        "envelope": {
                            "status": "ok",
                            "result_type": "text",
                            "result_chars": 100,
                        },
                    }
                ],
            )
        ),
        encoding="utf-8",
    )

    rc = agg.main(
        [
            "--cells-dir",
            str(cells_dir),
            "--output-dir",
            str(output_dir),
            "--require-tool-envelope-health",
        ]
    )
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.err == ""
