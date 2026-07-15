# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from unittest.mock import MagicMock, patch

from codeminer.eval.materialization_trace import TraceRequest
from scripts.profiling.benchmark_materialized_trace import (
    _combine_resource_usage,
    _languages,
    _required_index_types,
    _resource_usage,
)


def test_required_index_types_are_inferred_from_trace():
    requests = [
        TraceRequest("dense", "dense", {"query": "q"}),
        TraceRequest("sparse", "bm25", {"query": "q"}),
        TraceRequest("nav", "definition", {"symbol": "f"}),
    ]

    assert _required_index_types(requests) == ["bm25", "symbol_graph", "vector"]


def test_languages_normalizes_slash_and_commas():
    assert _languages("typescript / javascript, typescript") == [
        "typescript",
        "javascript",
    ]


def test_resource_usage_records_cpu_and_cuda_peaks():
    process = MagicMock(ru_maxrss=123)
    children = MagicMock(ru_maxrss=45)
    torch = MagicMock()
    torch.cuda.is_available.return_value = True
    torch.cuda.get_device_name.return_value = "test GPU"
    torch.cuda.max_memory_allocated.return_value = 1000
    torch.cuda.max_memory_reserved.return_value = 2000

    with (
        patch(
            "scripts.profiling.benchmark_materialized_trace.resource.getrusage",
            side_effect=[process, children],
        ),
        patch.dict("sys.modules", {"torch": torch}),
    ):
        usage = _resource_usage(scope="test")

    assert usage["process_max_rss_bytes"] == 123 * 1024
    assert usage["max_child_rss_bytes"] == 45 * 1024
    assert usage["cuda_device"] == "test GPU"
    assert usage["cuda_peak_allocated_bytes"] == 1000
    assert usage["cuda_peak_reserved_bytes"] == 2000


def test_combine_resource_usage_keeps_cross_process_maxima():
    materialization = {
        "scope": "materialization",
        "process_max_rss_bytes": 500,
        "max_child_rss_bytes": 700,
        "cuda_device": "test GPU",
        "cuda_peak_allocated_bytes": 900,
        "cuda_peak_reserved_bytes": 1000,
    }
    final = {
        "scope": "replay",
        "process_max_rss_bytes": 600,
        "max_child_rss_bytes": 0,
        "cuda_device": "test GPU",
        "cuda_peak_allocated_bytes": 300,
        "cuda_peak_reserved_bytes": 400,
    }

    combined = _combine_resource_usage(materialization, final)

    assert combined["process_max_rss_bytes"] == 600
    assert combined["max_child_rss_bytes"] == 700
    assert combined["cuda_peak_allocated_bytes"] == 900
    assert combined["cuda_peak_reserved_bytes"] == 1000
    assert combined["materialization_checkpoint"] == materialization
    assert combined["final_process"] == final
