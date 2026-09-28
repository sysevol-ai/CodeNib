# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Bounded static Python indexing and saved Wiki graph projections.

Only captured Python source enters the indexer scratch directory. No project
configuration, environment, dependency installation, or repository code runs.
Compiler artifacts are temporary files; only bounded reader views survive as
Wiki outputs. This does not provide a retained index or a graph database.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from ..toolchains import managed_path, resolve_command

_INDEX_SECONDS = 60
_MAX_INDEX_BYTES = 16 * 1024 * 1024
_COVERAGE_NOTE = (
    "Python source relationships from SCIP. External packages, dynamic calls "
    "and kernels embedded in strings are not resolved."
)


def _run_index(command, directory, check):
    # The caller owns this group until exit. Stop/timeout kills descendants
    # before temporary source is removed; no detached work survives a run.
    env = {
        "PATH": managed_path(current=os.environ.get("PATH", "")),
        "HOME": str(directory),
        "NODE_OPTIONS": "--max-old-space-size=2048",
        "LANG": "C.UTF-8",
    }
    with subprocess.Popen(
        command,
        cwd=directory,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    ) as process:
        deadline = time.monotonic() + _INDEX_SECONDS
        try:
            while True:
                check()
                if time.monotonic() >= deadline:
                    raise TimeoutError("Python indexing reached its time limit")
                try:
                    return process.wait(timeout=0.25) == 0
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.wait()


def build_visitor_graph(bundle, check, progress):
    """Attach an actual compiler graph or a credential-free coverage reason."""
    reader = bundle.source_reader
    paths = sorted(p for p in reader.file_paths if Path(p).suffix in {".py", ".pyi"})
    coverage = {
        "available": False,
        "backend": "scip-python",
        "languages": ["python"] if paths else [],
        "note": _COVERAGE_NOTE,
        "reason": "unsupported_language",
    }
    bundle.graph_coverage = coverage
    if not paths:
        coverage["note"] = "Hosted code graphs currently cover Python source only."
        return
    tool = resolve_command("scip-python")
    if not tool:
        coverage.update(
            reason="tool_unavailable", note="The Python indexer is unavailable."
        )
        return
    progress("indexing", "")
    try:
        from ..scip_interface.scip_indexer_python import SCIPPythonIndexer

        with tempfile.TemporaryDirectory(prefix="codenib-wiki-graph-") as directory:
            scratch = Path(directory)
            root = scratch / "source"
            root.mkdir()
            with reader.read_session():
                for path in paths:
                    check()
                    target = root / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(
                        reader.read_prefix(path, max_bytes=4 * 1024 * 1024)
                    )
            (root / "pyrightconfig.json").write_text(
                '{"include":["**/*.py","**/*.pyi"],"exclude":[]}\n'
            )
            indexer = SCIPPythonIndexer(
                root, scratch / "index", decoder_backend="serial"
            )
            indexer._direct_indexer_path = tool
            command = indexer._build_index_command(project_name="visitor-wiki")
            command.extend(["--project-version", bundle.entry.base_commit])
            if not _run_index(command, root, check):
                raise ValueError("indexer failed")
            check()
            if not 0 < indexer.index_file.stat().st_size <= _MAX_INDEX_BYTES:
                raise ValueError("index size limit")
            if not indexer.decode_index():
                raise ValueError("invalid index")
            graph = indexer.process_index()
            check()
            if graph is None or graph.graph.vcount() <= 1:
                raise ValueError("empty graph")
            bundle.code_graph = lambda: graph
            coverage.update(
                available=True,
                reason=None,
                files=len(paths),
                nodes=graph.graph.vcount(),
                edges=graph.graph.ecount(),
            )
    except TimeoutError:
        coverage.update(
            reason="timeout",
            note="Code indexing reached its 60-second limit. The Wiki text is still available.",
        )
    except Exception:
        coverage.update(
            reason="index_failed",
            note="Code indexing could not finish. The Wiki text is still available.",
        )


def wiki_graph_views(bundle, tree, pages, check):
    """Project the same graph and source into the existing reader contracts."""
    from ..web.static_export import _area_map, _embed_page_graph_sources, _page_graph
    from .builder import WikiBuilder

    coverage = getattr(
        bundle,
        "graph_coverage",
        {
            "available": False,
            "reason": "not_indexed",
            "note": "This Wiki was generated without a code index.",
        },
    )
    graph = bundle.code_graph() if hasattr(bundle, "code_graph") else None
    if graph is None:
        return {
            "coverage": coverage,
            "system_map": {
                "available": False,
                "areas": [],
                "links": [],
                "reason": "graph_unavailable",
            },
        }, {}
    bundle.hierarchical_graph = lambda: None
    builder = WikiBuilder(bundle)
    views = {}
    for page in pages:
        check()
        view = _page_graph(bundle, page)
        views[page["id"]] = _embed_page_graph_sources(builder, view)
    check()
    return {"coverage": coverage, "system_map": _area_map(bundle, tree, pages)}, views
