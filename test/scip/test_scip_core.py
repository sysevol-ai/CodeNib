# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""C++ ``core/`` decoder parity test (pybind11).

For each language with a C++ decoder (python, go, rust, ts):

  1. Locate the serial reference graph at ``~/.codeminer/<instance_id>/graph.pkl``
     and the decoded SCIP index at ``~/.codeminer/<instance_id>/index.decoded``.
     Both are produced by the ``integration-serial`` CI job
     (``test_scip_multilingual`` for go/rust/ts, ``test_scip_swebench`` for
     python's sympy instance).
  2. Run the C++ core decoder on the same ``index.decoded``.
  3. Assert names + edges + per-vertex attributes (type, file,
     start/end_line, unified_name) are bit-for-bit identical.

This test intentionally does **not** rebuild the serial graph — it depends on
the ``integration-serial → scip-core`` job chain in ``ci.yml`` to have
populated ``graph.pkl``. If the cache is missing, the test fails under CI
(``$CI`` set) since that indicates an ``integration-serial`` regression, and
skips locally with a pointer to what should have produced it. Running core's
``process_index`` with ``output_file=None`` avoids clobbering the serial
``graph.pkl``.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
from typing import Tuple

import pytest

pytestmark = pytest.mark.integration_serial


if importlib.util.find_spec("codeminer_core") is None:
    pytest.skip(
        "codeminer_core pybind module not built. "
        "cmake -S core -B build/core && cmake --build build/core && "
        "PYTHONPATH=build/core",
        allow_module_level=True,
    )

# Library load-order ritual: codeminer_core links system libigraph while
# Python's `igraph` package bundles its own; whichever is loaded first wins
# the dynamic-linker race for shared symbols. We have to load codeminer_core
# BEFORE anything that transitively imports `igraph` (CodeGraph, ROISubgraph,
# the patcher tree, …) or `decode_scip` segfaults inside libigraph. The
# integration-serial CI job sequences fixtures so this order falls out
# naturally; in unit-test runs we have to enforce it explicitly here.
import codeminer_core  # noqa: E402, F401

_MULTILINGUAL_KEYWORDS = {
    "go": ["caddyserver/", "gin-gonic/", "gohugoio/", "hashicorp/", "prometheus/"],
    "rust": ["astral-sh/ruff", "pola-rs/polars", "tokio-rs/", "rust-lang/"],
    "ts": [
        "axios/",
        "vuejs/",
        "mui/",
        "darkreader/",
        "sveltejs/",
        "expressjs/",
        "insomnia/",
        "dayjs/",
    ],
}

_PYTHON_INSTANCE_FILTER = "^(sympy__sympy-21847)$"


def _pick_instance(language: str) -> Tuple[object, dict]:
    """Return (dataset_obj, instance_row) for the pinned fixture instance.

    Must match the instance used by ``test_scip_multilingual`` (for
    go/rust/ts) and ``test_scip_swebench`` (for python), since we rely on
    ``~/.codeminer/<instance_id>/`` outputs produced by those tests.
    """
    if language == "python":
        from codeminer.dataset.swebench import SwebenchDataset

        args = argparse.Namespace(
            model="gpt-4o",
            dataset="princeton-nlp/SWE-bench_Lite",
            split="test",
            filter_instance=_PYTHON_INSTANCE_FILTER,
        )
        dataset_obj = SwebenchDataset.from_args(args)
        rows = dataset_obj.load()
        if not rows:
            raise RuntimeError(
                f"No SWE-bench_Lite instance matched {_PYTHON_INSTANCE_FILTER}"
            )
        return dataset_obj, dict(rows[0])

    from codeminer.dataset.swebench_multilingual import SwebenchMultilingualDataset

    dataset_obj = SwebenchMultilingualDataset(split="test", filter_instance=".*")
    rows = dataset_obj.load()
    for row in rows:
        if any(k in row["repo"] for k in _MULTILINGUAL_KEYWORDS[language]):
            return dataset_obj, dict(row)
    raise RuntimeError(f"No SWE-bench_Multilingual instance for {language}")


def _run_parity(language: str) -> None:
    try:
        dataset, instance = _pick_instance(language)
    except Exception as exc:
        pytest.skip(f"[{language}] dataset unavailable: {exc}")

    dataset.process_instance(instance)  # idempotent: no-op if repo already cloned
    repo_path = Path(dataset.get_repo_path(instance))
    output_dir = Path.home() / ".codeminer" / instance["instance_id"]

    graph_pkl = output_dir / "graph.pkl"
    decoded = output_dir / "index.decoded"
    if not graph_pkl.exists() or not decoded.exists():
        msg = (
            f"[{language}] cache missing at {output_dir}. "
            f"graph.pkl={graph_pkl.exists()}, index.decoded={decoded.exists()}. "
            "integration-serial job should have produced these "
            "(test_scip_multilingual or test_scip_swebench)."
        )
        if os.getenv("CI"):
            pytest.fail(msg)
        pytest.skip(msg)

    from codeminer.graph.code_graph import CodeGraph

    # Rebuild the serial graph in-place if the cached pickle is at an older
    # schema than what we now expect. The expensive ``index.decoded`` file is
    # reused via ``skip_level="decode"``. Without this, a CodeGraph schema
    # bump silently breaks all parity runs until each cache is manually
    # cleared.
    try:
        serial_graph = CodeGraph.load_graph(str(graph_pkl))
    except ValueError as exc:
        if "schema_version" not in str(exc):
            raise
        from codeminer.ls_router import LSIndexer

        rebuild_indexer = LSIndexer(
            project_root=repo_path,
            output_dir=output_dir,
            language=language,
            decoder_backend="serial",
        )
        kwargs = {"infer_tsconfig": True} if language == "ts" else {}
        rebuilt = rebuild_indexer.run_pipeline(
            skip_level="decode", report_profile=False, **kwargs
        )
        assert (
            rebuilt is not None
        ), f"[{language}] failed to rebuild serial graph after stale-schema load"
        serial_graph = CodeGraph.load_graph(str(graph_pkl))

    # Core decoder: reuse index.decoded, don't clobber graph.pkl (serial's).
    from codeminer.graph.signature import assert_graph_signatures_equal
    from codeminer.ls_router import LSIndexer

    core_indexer = LSIndexer(
        project_root=repo_path,
        output_dir=output_dir,
        language=language,
        decoder_backend="core",
    )
    core_graph = core_indexer.process_index(output_file=None)
    assert core_graph is not None, f"[{language}] core process_index returned None"

    assert_graph_signatures_equal(serial_graph, core_graph, language)


# --------------------------------------------------------------------------
# Tests — one per SCIP language supported by the C++ core decoder.
# --------------------------------------------------------------------------


def test_core_go():
    _run_parity("go")


def test_core_rust():
    _run_parity("rust")


def test_core_ts():
    _run_parity("ts")


def test_core_python():
    _run_parity("python")
