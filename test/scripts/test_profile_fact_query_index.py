# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the reproducible FactQueryIndex promotion profiler."""

from __future__ import annotations

import gc
import importlib.util
import json
from pathlib import Path

import pytest

if importlib.util.find_spec("codenib_core") is None:
    pytest.skip(
        "codenib_core pybind module not built; run make core-build",
        allow_module_level=True,
    )

import codenib_core  # noqa: E402

from scripts.profiling.profile_fact_query_index import (  # noqa: E402
    _even_sample,
    _timed,
    main,
    profile_fact_query_index,
)

_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "scip/fixtures/typescript_schema_v5/index.decoded"
)


def test_profiler_records_graph_free_parity_raw_samples_and_arm_order() -> None:
    report = profile_fact_query_index(
        index_path=_FIXTURE,
        language="typescript",
        iterations=2,
        warmups=0,
        query_workload_size=4,
        parity_sample_limit=4,
    )

    assert report["schema_version"] == 1
    assert report["benchmark"] == "native_fact_query_index_v1"
    assert report["parity"] == {"passed": True, "error": None}
    assert report["capabilities"]["position_queries"] is False
    assert report["configuration"]["fact_query_index_contract"] == (
        codenib_core.fact_query_index_contract()
    )
    assert report["configuration"]["first_arm_by_iteration"] == [
        "legacy",
        "native",
    ]
    assert len(report["raw_samples"]["legacy_code_graph"]["total"]) == 2
    assert len(report["raw_samples"]["native_fact_query_index"]["total"]) == 2
    assert report["native_fact_query_index"]["native_index"]["samples"] == 2
    assert report["decision"]["parity"] is True


def test_profiler_can_apply_cold_start_gate() -> None:
    report = profile_fact_query_index(
        index_path=_FIXTURE,
        language="typescript",
        iterations=1,
        warmups=0,
        external_index_seconds=10.0,
        query_workload_size=2,
        parity_sample_limit=2,
    )

    assert report["decision"]["gate"] == "cold_start_end_to_end"
    assert report["decision"]["external_index_seconds"] == 10.0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    (
        ({"iterations": 0}, "iterations must be positive"),
        ({"warmups": -1}, "iterations must be positive"),
        ({"query_workload_size": 0}, "sample sizes must be positive"),
        ({"parity_sample_limit": 0}, "sample sizes must be positive"),
    ),
)
def test_profiler_rejects_invalid_configuration(kwargs, message) -> None:
    configuration = {
        "index_path": _FIXTURE,
        "language": "typescript",
        "iterations": 1,
        "warmups": 0,
    }
    configuration.update(kwargs)
    with pytest.raises(ValueError, match=message):
        profile_fact_query_index(**configuration)


def test_timed_restores_enabled_gc_after_success_and_failure() -> None:
    gc.enable()
    _, elapsed = _timed(lambda: "ok")
    assert elapsed >= 0
    assert gc.isenabled() is True

    with pytest.raises(RuntimeError, match="boom"):
        _timed(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert gc.isenabled() is True


def test_timed_preserves_disabled_gc_state() -> None:
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        _timed(lambda: None)
        assert gc.isenabled() is False
    finally:
        if was_enabled:
            gc.enable()


def test_even_sample_is_deterministic_and_validated() -> None:
    assert _even_sample(["a", "b", "c", "d", "e"], 2) == ["a", "c"]
    with pytest.raises(ValueError, match="positive"):
        _even_sample(["a"], 0)


def test_cli_writes_reproducible_json(tmp_path, capsys) -> None:
    output = tmp_path / "fact-query.json"
    result = main(
        [
            "--index",
            str(_FIXTURE),
            "--language",
            "typescript",
            "--iterations",
            "1",
            "--warmups",
            "0",
            "--query-workload-size",
            "2",
            "--parity-sample-limit",
            "2",
            "--output-json",
            str(output),
        ]
    )

    assert result == 0
    expected = json.loads(capsys.readouterr().out)
    assert json.loads(output.read_text(encoding="utf-8")) == expected
