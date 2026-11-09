# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Bounded static Python indexing and saved Wiki graph projections.

Only captured Python source enters the indexer scratch directory. No project
configuration, environment, dependency installation, or repository code runs.
Compiler artifacts are temporary files; only bounded reader views survive as
Wiki outputs. This does not provide a retained index or a graph database.

The provider is the one every other Python graph uses: pyrefly when its
pinned release is installed, scip-python when it is not or when
``CODENIB_PYTHON_GRAPH_PROVIDER=scip-python`` pins it.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from ..log_utils import get_logger
from ..scip_interface.pyrefly_provider import (
    PYREFLY_PROVIDER,
    SCIP_PYTHON_PROVIDER,
    select_python_graph_provider,
)
from ..toolchains import managed_path, resolve_command

_INDEX_SECONDS = 60
_MAX_INDEX_BYTES = 16 * 1024 * 1024
logger = get_logger(__name__)
_COVERAGE_NOTE = (
    "Python source relationships from static analysis. External packages, "
    "dynamic calls and kernels embedded in strings are not resolved."
)


class _IndexUnusable(Exception):
    """A fixed, content-free reason the compiler index cannot be used."""


def _index_path() -> str:
    return managed_path(current=os.environ.get("PATH", ""))


def _missing_interpreter(tool: str, path: str):
    """The interpreter a script's ``#!`` line names, when it cannot be found.

    scip-python is a Node.js script. A service whose PATH lacks ``node`` finds
    the script, but the script exits at once with status 127.
    """
    try:
        with open(tool, "rb") as handle:
            first = handle.readline(256)
    except OSError:
        return None
    if not first.startswith(b"#!"):
        return None
    words = first[2:].decode("utf-8", "ignore").split()
    if words and Path(words[0]).name == "env":
        words = [word for word in words[1:] if not word.startswith("-")]
    if not words:
        return None
    name = words[0]
    if os.path.isabs(name):
        found = os.access(name, os.X_OK)
    else:
        found = shutil.which(name, path=path)
    return None if found else Path(name).name


def _run_index(command, directory, check) -> int:
    """Run the indexer and return its exit status."""
    # The caller owns this group until exit. Stop/timeout kills descendants
    # before temporary source is removed; no detached work survives a run.
    env = {
        "PATH": _index_path(),
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
                    return process.wait(timeout=0.25)
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


def _index_with_pyrefly(executable, root, scratch, indexer, project_version, check):
    """Write ``indexer.index_file`` from a pyrefly Glean report.

    pyrefly checks a private config naming the captured files, against a
    standard-library-only interpreter. The report is read in this process:
    worker processes would inherit the server's environment.
    """
    from ..scip_interface.pyrefly_glean import glean_to_scip_index, read_glean_report
    from ..scip_interface.pyrefly_provider import (
        pyrefly_command,
        pyrefly_version,
        write_pyrefly_config,
    )
    from ..scip_interface.scip_indexer_python import _stdlib_only_python_shims

    config = write_pyrefly_config(scratch / "pyrefly.toml", root)
    glean = scratch / "glean"
    with _stdlib_only_python_shims(scratch) as shims:
        interpreter = str(Path(shims) / "python3") if shims else None
        command = pyrefly_command(executable, config, glean, interpreter=interpreter)
        status = _run_index(command, root, check)
    # Type errors exit 1 with a complete report.
    if status not in (0, 1) or not glean.is_dir():
        raise _IndexUnusable(f"indexer exited with status {status}")
    check()
    index, _stats = glean_to_scip_index(
        glean,
        root,
        project_name="visitor-wiki",
        project_version=project_version,
        tool_version=pyrefly_version(executable),
        report=read_glean_report(glean, workers=1),
    )
    data = index.SerializeToString()
    if not 0 < len(data) <= _MAX_INDEX_BYTES:
        raise _IndexUnusable("index size limit")
    indexer.index_file.parent.mkdir(parents=True, exist_ok=True)
    indexer.index_file.write_bytes(data)


def _index_with_scip_python(tool, root, indexer, project_version, check):
    """Write ``indexer.index_file`` with scip-python."""
    # Pyright matches an include glob such as "**/*.py" against root-level
    # files only, so a src/ or package layout indexed nothing. The scratch
    # root holds only captured Python source.
    (root / "pyrightconfig.json").write_text('{"include":["."],"exclude":[]}\n')
    indexer._direct_indexer_path = tool
    command = indexer._build_index_command(project_name="visitor-wiki")
    command.extend(["--project-version", project_version])
    status = _run_index(command, root, check)
    if status != 0:
        # The status alone is logged; indexer output can quote source.
        raise _IndexUnusable(f"indexer exited with status {status}")
    check()
    if not 0 < indexer.index_file.stat().st_size <= _MAX_INDEX_BYTES:
        raise _IndexUnusable("index size limit")


def _indexer_tool(coverage):
    """Return ``(provider, executable)``, or ``None`` with ``coverage`` updated."""
    provider = select_python_graph_provider()
    if provider.name == PYREFLY_PROVIDER:
        if provider.executable:
            return PYREFLY_PROVIDER, provider.executable
        # Pinned to pyrefly, which cannot run here.
        coverage.update(
            reason="tool_unavailable", note="The Python indexer is unavailable."
        )
        return None
    tool = resolve_command("scip-python")
    if not tool:
        coverage.update(
            reason="tool_unavailable", note="The Python indexer is unavailable."
        )
        return None
    missing = _missing_interpreter(tool, _index_path())
    if missing:
        # Say so instead of starting a process that can only fail.
        logger.warning(
            "Visitor Wiki code indexing skipped: %s is not on the server PATH",
            missing,
        )
        coverage.update(
            reason="tool_unavailable",
            note=f"The Python indexer needs {missing}, which this server cannot find.",
        )
        return None
    return SCIP_PYTHON_PROVIDER, tool


def build_visitor_graph(bundle, check, progress):
    """Attach an actual compiler graph or a credential-free coverage reason."""
    reader = bundle.source_reader
    paths = sorted(p for p in reader.file_paths if Path(p).suffix in {".py", ".pyi"})
    coverage = {
        "available": False,
        "backend": None,
        "languages": ["python"] if paths else [],
        "note": _COVERAGE_NOTE,
        "reason": "unsupported_language",
    }
    bundle.graph_coverage = coverage
    if not paths:
        coverage["note"] = "Hosted code graphs currently cover Python source only."
        return
    chosen = _indexer_tool(coverage)
    if chosen is None:
        return
    backend, tool = chosen
    coverage["backend"] = backend
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
            indexer = SCIPPythonIndexer(
                root, scratch / "index", decoder_backend="serial"
            )
            version = bundle.entry.base_commit
            if backend == PYREFLY_PROVIDER:
                _index_with_pyrefly(tool, root, scratch, indexer, version, check)
            else:
                _index_with_scip_python(tool, root, indexer, version, check)
            if not indexer.decode_index():
                raise _IndexUnusable("invalid index")
            graph = indexer.process_index()
            check()
            if graph is None or graph.graph.vcount() <= 1:
                raise _IndexUnusable("empty graph")
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
    except Exception as exc:
        # Our own reasons are fixed strings; other errors log their type only.
        logger.warning(
            "Visitor Wiki code indexing failed (%s)",
            exc if isinstance(exc, _IndexUnusable) else type(exc).__name__,
        )
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
