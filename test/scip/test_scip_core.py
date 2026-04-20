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
populated ``graph.pkl``. If the cache is missing, the test skips with a
pointer to what should have produced it. Running core's ``process_index``
with ``output_file=None`` avoids clobbering the serial ``graph.pkl``.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, Set, Tuple

import pytest

pytestmark = pytest.mark.integration_serial


try:
    import codeminer_core  # type: ignore[import-not-found]  # noqa: F401

    _PYBIND_AVAILABLE = True
except ImportError:
    _PYBIND_AVAILABLE = False

if not _PYBIND_AVAILABLE:
    pytest.skip(
        "codeminer_core pybind module not built. "
        "cmake -S core -B build/core && cmake --build build/core && "
        "PYTHONPATH=build/core",
        allow_module_level=True,
    )


_COMPARED_ATTRS = ("type", "file", "start_line", "end_line", "unified_name")


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


def _graph_signature(
    graph,
) -> Tuple[Set[str], Set[Tuple[str, str, str]], Dict[str, Dict]]:
    names = set(graph.vs["name"])
    i2n = {v.index: v["name"] for v in graph.vs}
    edges = {(i2n[e.source], i2n[e.target], e["type"]) for e in graph.es}
    attrs = {
        v["name"]: {k: v.attributes().get(k) for k in _COMPARED_ATTRS} for v in graph.vs
    }
    return names, edges, attrs


def _assert_graph_parity(ref, cand, tag: str) -> None:
    ref_names, ref_edges, ref_attrs = _graph_signature(ref.graph)
    cand_names, cand_edges, cand_attrs = _graph_signature(cand.graph)

    assert ref_names == cand_names, (
        f"[{tag}] name sets differ: "
        f"ref-only={len(ref_names - cand_names)}, "
        f"cand-only={len(cand_names - ref_names)}, "
        f"missing={sorted(ref_names - cand_names)[:3]}, "
        f"extra={sorted(cand_names - ref_names)[:3]}"
    )
    assert ref_edges == cand_edges, (
        f"[{tag}] edge sets differ: "
        f"ref-only={len(ref_edges - cand_edges)}, "
        f"cand-only={len(cand_edges - ref_edges)}"
    )
    per_attr = {
        a: sum(1 for n in ref_names if ref_attrs[n].get(a) != cand_attrs[n].get(a))
        for a in _COMPARED_ATTRS
    }
    assert all(
        c == 0 for c in per_attr.values()
    ), f"[{tag}] vertex attributes differ: {per_attr}"


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
        pytest.skip(
            f"[{language}] cache missing at {output_dir}. "
            f"graph.pkl={graph_pkl.exists()}, index.decoded={decoded.exists()}. "
            "integration-serial job should have produced these "
            "(test_scip_multilingual or test_scip_swebench)."
        )

    from codeminer.graph.code_graph import CodeGraph

    serial_graph = CodeGraph.load_graph(str(graph_pkl))

    # Core decoder: reuse index.decoded, don't clobber graph.pkl (serial's).
    from codeminer.ls_router import LSIndexer

    core_indexer = LSIndexer(
        project_root=repo_path,
        output_dir=output_dir,
        language=language,
        decoder_backend="core",
    )
    core_graph = core_indexer.process_index(output_file=None)
    assert core_graph is not None, f"[{language}] core process_index returned None"

    _assert_graph_parity(serial_graph, core_graph, language)


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
