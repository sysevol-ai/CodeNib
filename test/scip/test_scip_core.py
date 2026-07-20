# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""C++ ``core/`` decoder parity test (pybind11).

For each cached integration language with a C++ decoder (python, go, rust, ts):

  1. Locate the serial reference graph at ``~/.codeminer/<instance_id>/graph.pkl``
     and the decoded SCIP index at ``~/.codeminer/<instance_id>/index.decoded``.
     Both are produced by the ``integration-serial`` CI job
     (``test_scip_multilingual`` for go/rust/ts, ``test_scip_swebench`` for
     python's sympy instance).
  2. Run the C++ core decoder on the same ``index.decoded``.
  3. Assert names + edges + per-vertex attributes (type, file,
     start/end_line, selection location, unified_name) are bit-for-bit identical.

This test intentionally does **not** rebuild the serial graph — it depends on
the ``integration-serial → scip-core`` job chain in ``ci.yml`` to have
populated ``graph.pkl``. If the cache is missing, the test fails under CI
(``$CI`` set) since that indicates an ``integration-serial`` regression, and
skips locally with a pointer to what should have produced it. Running core's
``process_index`` with ``output_file=None`` avoids clobbering the serial
``graph.pkl``. Ruby also has a C++ decoder; its CI-stable parity coverage uses a
synthetic decoded index in this file because the real Bundler/rake fixture is a
local promotion gate rather than an integration-serial cache.
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

# Library load-order ritual: codeminer_core vendors c-igraph and hides its
# archive symbols so it does not fight Python's `igraph` wheel. Load it before
# any transitive `igraph` import anyway; older local builds may not have the
# same symbol isolation, and this keeps the optional-extension contract stable.
import codeminer_core  # noqa: E402, F401

_COMPARED_ATTRS = (
    "type",
    "file",
    "start_line",
    "end_line",
    "selection_line",
    "selection_character",
    "unified_name",
)

# Edge attributes compared for parity. `anchor_file` / `anchor_line` were
# added in schema v3 to support LSP-aligned line-range queries; both serial
# and core decoders thread the SCIP `occurrence.range` start line through
# reference edges.
_EDGE_COMPARED_ATTRS = ("type", "anchor_file", "anchor_line")


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

_RUBY_CORE_PARITY_INDEX = """
metadata {
  tool_info {
    name: "scip-ruby"
    version: "0.4.7"
  }
}
documents {
  relative_path: "lib/invoice.rb"
  occurrences {
    range: 0
    range: 7
    range: 12
    symbol: "scip-ruby gem smoke 0.1 Smoke#"
    symbol_roles: 1
  }
  occurrences {
    range: 1
    range: 8
    range: 15
    symbol: "scip-ruby gem smoke 0.1 Smoke#Invoice#"
    symbol_roles: 1
  }
  occurrences {
    range: 2
    range: 8
    range: 13
    symbol: "scip-ruby gem smoke 0.1 Smoke#Invoice#total()."
    symbol_roles: 1
  }
  occurrences {
    range: 7
    range: 11
    range: 20
    symbol: "scip-ruby gem smoke 0.1 `<Class:Smoke>`#normalize()."
    symbol_roles: 1
  }
  occurrences {
    range: 8
    range: 16
    range: 21
    symbol: "scip-ruby gem smoke 0.1 Smoke#Invoice#total()."
  }
  symbols {
    symbol: "scip-ruby gem smoke 0.1 Smoke#"
    documentation: "```ruby\\nmodule Smoke\\n```"
  }
  symbols {
    symbol: "scip-ruby gem smoke 0.1 Smoke#Invoice#"
    documentation: "```ruby\\nclass Smoke::Invoice\\n```"
  }
  symbols {
    symbol: "scip-ruby gem smoke 0.1 Smoke#Invoice#total()."
    documentation: "```ruby\\ndef total\\n```"
  }
  symbols {
    symbol: "scip-ruby gem smoke 0.1 `<Class:Smoke>`#normalize()."
    documentation: "```ruby\\ndef self.normalize\\n```"
  }
}
documents {
  relative_path: "lib/rake/file_list.rb"
  occurrences {
    range: 0
    range: 7
    range: 11
    symbol: "scip-ruby gem smoke 0.1 Rake#"
    symbol_roles: 1
  }
  occurrences {
    range: 1
    range: 8
    range: 16
    symbol: "scip-ruby gem smoke 0.1 Rake#FileList#"
    symbol_roles: 1
  }
  occurrences {
    range: 2
    range: 8
    range: 15
    symbol: "scip-ruby gem smoke 0.1 Rake#FileList#include()."
    symbol_roles: 1
  }
  occurrences {
    range: 6
    range: 8
    range: 13
    symbol: "scip-ruby gem smoke 0.1 Rake#FileList#is_a?()."
    symbol_roles: 1
  }
}
documents {
  relative_path: "lib/rake/application.rb"
  occurrences {
    range: 0
    range: 6
    range: 17
    symbol: "scip-ruby gem smoke 0.1 Rake#Application#"
    symbol_roles: 1
  }
  occurrences {
    range: 1
    range: 6
    range: 32
    symbol: "scip-ruby gem smoke 0.1 Rake#Application#load_debug_at_stop_feature()."
    symbol_roles: 1
    enclosing_range: 1
    enclosing_range: 2
    enclosing_range: 6
    enclosing_range: 0
  }
  occurrences {
    range: 3
    range: 10
    range: 17
    symbol: "scip-ruby gem smoke 0.1 Rake#Application#execute()."
    symbol_roles: 1
  }
  occurrences {
    range: 8
    range: 12
    range: 22
    symbol: "scip-ruby gem rake 13.3 Rake#Application#`tty_output=`()."
    symbol_roles: 1
  }
}
documents {
  relative_path: "lib/rake/phony.rb"
  occurrences {
    range: 1
    range: 4
    range: 18
    symbol: "scip-ruby gem smoke 0.1 `<Class:Object>`#timestamp()."
    symbol_roles: 1
  }
}
""".strip()


def _write_ruby_core_parity_project(project_root: Path) -> Path:
    (project_root / "lib/rake").mkdir(parents=True)
    (project_root / "lib/invoice.rb").write_text(
        "\n".join(
            [
                "module Smoke",
                "  class Invoice",
                "    def total",
                "      1",
                "    end",
                "  end",
                "",
                "  def self.normalize(value)",
                "    Invoice.new.total",
                "  end",
            ]
        ),
        encoding="utf-8",
    )
    (project_root / "lib/rake/file_list.rb").write_text(
        "\n".join(
            [
                "module Rake",
                "  class FileList",
                "    def include(*filenames)",
                "    end",
                "    alias :add :include",
                "",
                "    def is_a?(klass)",
                "    end",
                "    alias kind_of? is_a?",
                "  end",
                "end",
            ]
        ),
        encoding="utf-8",
    )
    (project_root / "lib/rake/application.rb").write_text(
        "\n".join(
            [
                "class Application",
                "  def load_debug_at_stop_feature",
                "    Module.new do",
                "      def execute(*)",
                "      end",
                "    end",
                "  end",
                "  attr_writer :tty_output",
                "end",
            ]
        ),
        encoding="utf-8",
    )
    (project_root / "lib/rake/phony.rb").write_text(
        "task = Object.new\n" "def task.timestamp\n" "end\n",
        encoding="utf-8",
    )
    index = project_root / "index.decoded"
    index.write_text(_RUBY_CORE_PARITY_INDEX, encoding="utf-8")
    return index


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


def _graph_signature(graph):
    """Return (names, edge_multiset, vertex_attrs).

    ``edge_multiset`` is a sorted list of
    ``(src_name, tgt_name, type, anchor_file, anchor_line)`` so multi-edges
    between the same `(src, tgt)` pair (one per call site) are preserved
    and order-independent comparison still works.
    """
    names = set(graph.vs["name"])
    i2n = {v.index: v["name"] for v in graph.vs}
    edges_list = []
    for e in graph.es:
        attrs = e.attributes()
        edges_list.append(
            (
                i2n[e.source],
                i2n[e.target],
                attrs.get("type"),
                attrs.get("anchor_file"),
                attrs.get("anchor_line"),
            )
        )
    edges_list.sort(key=lambda t: (t[0], t[1], t[2] or "", t[3] or "", t[4] or -1))
    vertex_attrs = {
        v["name"]: {k: v.attributes().get(k) for k in _COMPARED_ATTRS} for v in graph.vs
    }
    return names, edges_list, vertex_attrs


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

    if ref_edges != cand_edges:
        # Build category breakdown for a useful failure message.
        from collections import Counter

        ref_set = set(ref_edges)
        cand_set = set(cand_edges)
        ref_only = ref_set - cand_set
        cand_only = cand_set - ref_set
        per_field = {field: 0 for field in _EDGE_COMPARED_ATTRS}
        # Count mismatches grouped by which field disagrees, on a sample.
        for e in list(ref_only)[:200]:
            for c in list(cand_only)[:200]:
                if e[0] == c[0] and e[1] == c[1]:
                    if e[2] != c[2]:
                        per_field["type"] += 1
                    if e[3] != c[3]:
                        per_field["anchor_file"] += 1
                    if e[4] != c[4]:
                        per_field["anchor_line"] += 1
                    break

        # Multi-edge multiplicity diff: same edge tuple appearing more / fewer
        # times. Surfaces dedup mismatches between serial and core.
        ref_counts = Counter(ref_edges)
        cand_counts = Counter(cand_edges)
        mult_diff = []
        for e in set(ref_counts) | set(cand_counts):
            r = ref_counts.get(e, 0)
            c = cand_counts.get(e, 0)
            if r != c:
                mult_diff.append((e, r, c))

        sample = mult_diff[:5]
        raise AssertionError(
            f"[{tag}] edge sets differ "
            f"(ref={len(ref_edges)} cand={len(cand_edges)}): "
            f"ref-only={len(ref_only)}, cand-only={len(cand_only)}, "
            f"multiplicity-mismatches={len(mult_diff)}, "
            f"sample-field-mismatches={per_field}, "
            f"sample-multi-diff={sample}"
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


def test_core_ruby_synthetic_parity(tmp_path):
    index = _write_ruby_core_parity_project(tmp_path)

    from codeminer.scip_interface.scip_decode_core import SCIPDecoderCore
    from codeminer.scip_interface.scip_decode_ruby import SCIPRubyGraphDecoder

    serial_graph = SCIPRubyGraphDecoder(str(index), project_root=str(tmp_path)).decode()
    core_graph = SCIPDecoderCore(
        str(index),
        project_root=str(tmp_path),
        language="ruby",
    ).decode()

    _assert_graph_parity(serial_graph, core_graph, "ruby-synthetic")


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
