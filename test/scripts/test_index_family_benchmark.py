# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from scripts.retrieval_ablation import index_family_benchmark as benchmark


def test_adaptive_nlist_respects_training_population():
    assert benchmark.adaptive_nlist(171) == 4
    assert benchmark.adaptive_nlist(3773) == 96
    assert benchmark.adaptive_nlist(27_981) == 669


def test_ranked_files_and_task_recall_use_unique_suffix_matches():
    documents = [
        SimpleNamespace(metadata={"file": "/cache/repo/src/a.py"}),
        SimpleNamespace(metadata={"file": "/cache/repo/src/a.py"}),
        SimpleNamespace(metadata={"file": "/cache/repo/src/b.py"}),
    ]

    ranked = benchmark._ranked_unique_files(documents, [0, 1, 2])

    assert ranked == ["cache/repo/src/a.py", "cache/repo/src/b.py"]
    assert benchmark._task_metrics(ranked, ["src/b.py"], 2) == {
        "hits": 1,
        "targets": 1,
        "recall": 1.0,
        "all_targets_hit": 1,
    }


def test_ann_recall_is_measured_against_flat_neighbors():
    assert benchmark._ann_recall([0, 2, 4, 6], [0, 1, 2, 3], 4) == 0.5


def test_parameter_summary_distinguishes_fixed_and_adaptive_values():
    rows = [
        {"parameters": {"probe_fraction": 0.25, "nlist": 4}},
        {"parameters": {"probe_fraction": 0.25, "nlist": 8}},
    ]

    assert benchmark._summarize_parameters(rows) == {
        "nlist": {"min": 4, "median": 6.0, "max": 8},
        "probe_fraction": 0.25,
    }


def test_small_index_family_benchmark_has_exact_full_probe_ivf():
    rng = np.random.default_rng(7)
    vectors = rng.normal(size=(160, 16)).astype(np.float32)
    documents = [
        SimpleNamespace(metadata={"file": f"src/file_{index}.py"})
        for index in range(len(vectors))
    ]
    query = vectors[11]

    methods, derived = benchmark.benchmark_vectors(
        vectors=vectors,
        documents=documents,
        query_vector=query,
        targets=["src/file_11.py"],
        search_k=10,
        file_k=10,
        neighbor_k=10,
        warmups=1,
        repetitions=2,
        ivf_probe_fractions=[0.5, 1.0],
        hnsw_m=8,
        hnsw_ef_construction=32,
        hnsw_ef_search=[16],
    )

    assert derived == {"nlist": 4}
    assert methods["flat"]["ann_recall_at_k"] == 1.0
    assert methods["ivf-100pct"]["ann_recall_at_k"] == 1.0
    assert methods["flat"]["task"]["recall"] == 1.0
    assert methods["hnsw-ef16"]["search"]["count"] == 2
