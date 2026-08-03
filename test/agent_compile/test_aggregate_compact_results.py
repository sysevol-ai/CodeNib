# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for compact preload result aggregation."""

from __future__ import annotations

import json

from scripts.agent_compile import aggregate_compact_results as agg


def _write_cell(root, sweep: str, subset: str) -> None:
    cells = root / sweep / "cells"
    cells.mkdir(parents=True, exist_ok=True)
    cell = {
        "success": True,
        "metrics_meaningful": True,
        "subset_id": subset,
        "instance_id": "inst-1",
        "query_id": "query-1",
        "metrics": {
            "files": {"5": {"recall": 1.0}},
            "answer_blocks": {"5": {"recall": 0.5}},
        },
        "total_tokens": 123,
        "total_turns": 2,
    }
    (cells / "cell.json").write_text(json.dumps(cell), encoding="utf-8")


def _write_idxcmp(root, name: str, rows) -> None:
    (root / f"idxcmp_{name}.json").write_text(json.dumps(rows), encoding="utf-8")


def test_table2_embedding_reads_recall_from_idxcmp(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(agg, "RESULTS", str(tmp_path))
    _write_cell(tmp_path, "haiku_base_compact", "preinj_eager_compact")
    _write_idxcmp(
        tmp_path,
        "Qwen_Qwen3-Embedding-0.6B",
        [
            {"n_targets": 1, "recall": {"flat": {"files@10": True}}},
            {"n_targets": 1, "recall": {"flat": {"files@10": False}}},
        ],
    )

    agg.table2_embedding()

    out = capsys.readouterr().out
    assert "| Qwen3-Embedding-0.6B | 50% | 1.000 | 0.500 | 123 |" in out


def test_table3_uses_qwen_4b_keepreads_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(agg, "RESULTS", str(tmp_path))
    _write_cell(tmp_path, "qwen_4b_keepreads", "grep_only")

    agg.table3_dial()

    out = capsys.readouterr().out
    assert "| grep | 1 | 1.000 | 0.500 | 123 | 2.0 |" in out


def test_table5_recall_handles_missing_k(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(agg, "RESULTS", str(tmp_path))
    _write_idxcmp(
        tmp_path,
        "Qwen_Qwen3-Embedding-0.6B",
        [
            {
                "n_targets": 1,
                "latency_ms": {"flat": 1.25, "scoped": 2.5},
                "recall": {
                    "flat": {
                        "files@1": True,
                        "files@5": True,
                        "files@10": False,
                    },
                    "scoped": {
                        "files@1": False,
                        "files@5": True,
                        "files@10": True,
                    },
                },
            }
        ],
    )

    agg.table5_recall()

    out = capsys.readouterr().out
    assert (
        "| Qwen_Qwen3-Embedding-0.6B | flat | 100% | 100% | 0% | n/a | 1.2ms |" in out
    )
    assert (
        "| Qwen_Qwen3-Embedding-0.6B | scoped | 0% | 100% | 100% | n/a | 2.5ms |" in out
    )
