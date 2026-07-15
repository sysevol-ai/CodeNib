# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from scripts.profiling.build_materialized_trace_plan import _load_dataset, _lsp_requests


def _report(*, eligible: set[int], invalid_line_index: int | None = None) -> dict:
    requests = [
        {
            "request_id": f"request-{index}",
            "capability": "definition" if index % 2 == 0 else "references",
            "arguments": {
                "file_path": "src/example.py",
                "line": -1 if index == invalid_line_index else index,
                "character": 3,
            },
        }
        for index in range(10)
    ]
    comparisons = []
    for index, request in enumerate(requests):
        for repetition in range(2):
            equivalent = index in eligible
            fingerprint = f"{index + 1:064x}"
            comparisons.append(
                {
                    "request": request,
                    "rep": repetition + 1,
                    "same_result": equivalent,
                    "static_call": {
                        "status": "ok",
                        "result_count": 1,
                        "result_fingerprint": fingerprint,
                    },
                    "reference_call": {
                        "status": "ok",
                        "result_count": 1 if equivalent else 2,
                        "result_fingerprint": (
                            fingerprint if equivalent else f"{index + 2:064x}"
                        ),
                    },
                }
            )
    return {
        "subject": {"snapshot_id": "snapshot-a"},
        "requests": requests,
        "comparisons": comparisons,
    }


def test_lsp_requests_convert_json_rpc_lines_to_agent_lines():
    source = _report(eligible={0, 1, 2, 3, 4})

    requests = _lsp_requests(source)

    assert len(requests) == 2
    for request in requests:
        source_index = int(request["request_id"].rsplit("-", 1)[-1])
        assert request["params"]["line"] == source_index + 1
        assert request["expected_result"]["result_count"] == 1
        assert len(request["expected_result"]["fingerprint"]) == 64
    assert {request["operation"] for request in requests} == {"definition"}
    assert source["requests"][0]["arguments"]["line"] == 0


def test_lsp_requests_exclude_non_equivalent_candidates():
    source = _report(eligible={2, 3, 4})

    requests = _lsp_requests(source)

    assert {request["request_id"] for request in requests} == {
        "lsp:request-2",
        "lsp:request-4",
    }


def test_lsp_requests_reject_invalid_json_rpc_line():
    source = _report(eligible={0, 2}, invalid_line_index=0)

    with pytest.raises(ValueError, match="invalid zero-based LSP line"):
        _lsp_requests(source)


def test_lsp_requests_require_two_admitted_definitions():
    source = _report(eligible={0, 1})

    with pytest.raises(ValueError, match="at least two"):
        _lsp_requests(source)


def test_load_dataset_pins_revision_for_config_and_rows(monkeypatch):
    calls = []

    def fake_configs(name, *, revision):
        calls.append(("configs", name, revision))
        return ["python", "rust"]

    def fake_load(name, config, *, split, revision):
        calls.append(("load", name, config, split, revision))
        return [{"config": config}]

    monkeypatch.setattr("datasets.get_dataset_config_names", fake_configs)
    monkeypatch.setattr("datasets.load_dataset", fake_load)

    rows = _load_dataset("org/dataset", "test", "dataset-commit")

    assert rows == [{"config": "python"}, {"config": "rust"}]
    assert calls == [
        ("configs", "org/dataset", "dataset-commit"),
        ("load", "org/dataset", "python", "test", "dataset-commit"),
        ("load", "org/dataset", "rust", "test", "dataset-commit"),
    ]
