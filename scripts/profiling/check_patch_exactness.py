#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Is a patched graph the graph a fresh build gives?

For one commit of a repository: build the graph at its parent, patch it to the
commit, build fresh at the commit, and compare the facts the two graphs state
(``FactProjectionVerifier``). Prints one JSON line.

    python scripts/profiling/check_patch_exactness.py REPO COMMIT \\
        [--language python] [--patch builder|lsp] [--work DIR]

``builder`` rebuilds the changed files with the graph's builder (Python with
pyrefly). ``lsp`` uses the language-server patcher. The repository is checked
out at the parent and then at the commit, so run it on a clone you can move.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _build(repo: Path, output: Path, language: str):
    from codenib.ls_router import LSIndexer

    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True)
    indexer = LSIndexer(project_root=repo, output_dir=output, language=language)
    started = time.monotonic()
    graph = indexer.run_pipeline(
        output_file=str(output / "graph.pkl"),
        skip_level=None,
        reset_profiler=True,
        report_profile=False,
    )
    if graph is None:
        raise SystemExit(f"no {language} graph could be built in {repo}")
    return indexer, graph, time.monotonic() - started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", type=Path)
    parser.add_argument("commit")
    parser.add_argument("--language", default="python")
    parser.add_argument("--patch", choices=("builder", "lsp"), default="lsp")
    parser.add_argument("--work", type=Path)
    args = parser.parse_args()

    from codenib.compiler.verification import FactProjectionVerifier

    repo = args.repo.resolve()
    target = _git(repo, "rev-parse", args.commit)
    base = _git(repo, "rev-parse", f"{target}~1")
    work = args.work or Path(tempfile.mkdtemp(prefix="codenib-patch-exactness-"))

    _git(repo, "checkout", "-q", "--detach", base)
    indexer, graph, _ = _build(repo, work / "base", args.language)
    _git(repo, "checkout", "-q", "--detach", target)
    started = time.monotonic()
    if args.patch == "builder":
        from codenib.graph.incremental.builder_patch import rebuild_changed_files
        from codenib.graph.incremental.change_mgr import detect_changed_files
        from codenib.scip_interface.pyrefly_glean import SYMBOL_TABLE_NAME

        rebuild_changed_files(
            graph,
            repo,
            detect_changed_files(str(repo), base, target, extensions={".py"}),
            symbol_table=work / "base" / SYMBOL_TABLE_NAME,
        )
    else:
        indexer.graph_patch(graph, base, target)
    patch_seconds = time.monotonic() - started
    _, fresh, fresh_seconds = _build(repo, work / "fresh", args.language)

    result = FactProjectionVerifier().verify(
        index_type="symbol_graph", patched=graph, fresh=fresh
    )
    print(
        json.dumps(
            {
                "commit": target,
                "language": args.language,
                "patch": args.patch,
                "equal": result.verified,
                **result.details,
                "patch_seconds": round(patch_seconds, 1),
                "fresh_seconds": round(fresh_seconds, 1),
            }
        )
    )
    return 0 if result.verified else 1


if __name__ == "__main__":
    sys.exit(main())
