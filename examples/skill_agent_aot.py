#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""End-to-end demo: AoT (ahead-of-time) compile + agent query.

Two-phase architecture using the high-level public API from
``codenib.agent``:

    Phase 1 — ``compile_repo(...)``                  (run once per repo)
              writes BM25 / vector / symbol-graph artifacts plus
              ``repo_manifest.json`` under ``<cache_dir>/``.

    Phase 2 — ``query(prompt, options=...)``         (run many times)
              loads the manifest, registers skills, drives the LLM-driven
              agent loop. No inline re-indexing.

This is the **high-level** counterpart to ``examples/skill_agent.py``:
that file shows the same two-phase pattern but wires up
``IndexCompiler``, ``BM25CodeIndexer().load_index(...)``, and
``AgentRunner`` by hand. The two coexist intentionally — start here for
the simple path, drop down to ``skill_agent.py`` for custom builder
registries / partial rebuilds.

Usage::

    # Phase 1 only (no LLM credentials needed; just builds + prints):
    python examples/skill_agent_aot.py --no-llm

    # Phase 1 + Phase 2 against the codenib repo itself, using Vertex AI:
    export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json
    python examples/skill_agent_aot.py \\
        --repo . \\
        --query "where is bm25 indexing implemented?" \\
        --model vertex_ai/gemini-2.5-flash

    # Use your own repo:
    python examples/skill_agent_aot.py \\
        --repo /path/to/your/repo \\
        --languages python javascript \\
        --query "how does authentication work?"
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from codenib.paths import repo_index_dir

# ---------------------------------------------------------------------------
# Pretty-printers — borrowed from examples/skill_agent.py:182 so the
# two demos render the same manifest summary.
# ---------------------------------------------------------------------------


def print_manifest(manifest) -> None:
    print("\n=== Repo Manifest ===")
    print(f"  repo      : {manifest.repo_path}")
    commit = manifest.commit[:12] if manifest.commit else "(none)"
    print(f"  commit    : {commit}")
    print(f"  languages : {manifest.languages}")
    print(f"  files     : {manifest.file_count}")
    print("\n  Indexes:")
    for idx_type, entry in manifest.indexes.items():
        print(f"    {idx_type}: [{entry.status}] {entry.path}")
        if entry.metadata.get("build_duration_seconds"):
            print(f"      built in {entry.metadata['build_duration_seconds']:.1f}s")
        if entry.metadata.get("document_count"):
            print(f"      documents: {entry.metadata['document_count']}")
    print(f"\n  Capabilities: {manifest.capabilities}")


def print_result(result, prompt: str) -> None:
    print("\n=== Agent Result ===")
    print(f"  prompt       : {prompt}")
    print(f"  turns        : {result.total_turns}")
    print(f"  tool calls   : {len(result.tool_calls)}")
    for i, tc in enumerate(result.tool_calls, 1):
        if tc.error:
            print(f"    [{i}] {tc.skill_id}({tc.arguments}) — ERROR: {tc.error}")
        else:
            n = len(tc.result) if isinstance(tc.result, list) else 1
            print(f"    [{i}] {tc.skill_id}({tc.arguments}) — {n} result(s)")
    if result.usage and result.usage.total_tokens:
        print(f"  tokens       : {result.usage.total_tokens}")
    print(f"\n  answer:\n{'-' * 60}\n{result.answer}\n{'-' * 60}")


# ---------------------------------------------------------------------------
# Phase 1: ahead-of-time index compilation
# ---------------------------------------------------------------------------


def phase_one_compile(
    repo_path: str,
    *,
    index_types: Sequence[str],
    languages: Sequence[str],
    cache_dir: str,
):
    """Build indexes once and write ``repo_manifest.json``."""
    from codenib.agent import compile_repo

    print(f"\n[Phase 1] Compiling indexes for {repo_path}")
    print(f"  index_types: {list(index_types)}")
    print(f"  languages  : {list(languages)}")
    print(f"  cache_dir  : {cache_dir}")

    manifest = compile_repo(
        repo_path,
        index_types=tuple(index_types),
        languages=tuple(languages),
        cache_dir=cache_dir,
    )
    print_manifest(manifest)
    return manifest


# ---------------------------------------------------------------------------
# Phase 2: agent query against the prebuilt manifest
# ---------------------------------------------------------------------------


def phase_two_query(
    manifest,
    *,
    prompt: str,
    model: str,
    allowed_skills: Sequence[str],
    primary_language: str,
    max_turns: int,
):
    """Run one query() call against the manifest. No rebuild."""
    from codenib.agent import CodeNibAgentOptions, query

    # A coherent compile_table demonstrating CAR per-query narrowing:
    # any scenario this user might land in collapses to bm25_search.
    # Union(values) == allowed_skills, so _warn_on_skill_set_mismatch
    # stays silent.
    compile_table = {
        f"{primary_language}:stacktrace": list(allowed_skills),
        f"{primary_language}:no_stacktrace": list(allowed_skills),
    }

    print(f"\n[Phase 2] Querying via {model}")
    print(f"  allowed_skills : {list(allowed_skills)}")
    print(f"  compile_table  : {len(compile_table)} scenario(s)")

    result = query(
        prompt,
        options=CodeNibAgentOptions(
            manifest=manifest,
            allowed_skills=list(allowed_skills),
            compile_table=compile_table,
            model=model,
            primary_language=primary_language,
            max_turns=max_turns,
        ),
    )
    print_result(result, prompt)
    return result


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _default_repo() -> str:
    """Default to the codenib repo itself so the demo runs self-contained."""
    return str(Path(__file__).resolve().parent.parent)


def parse_args(argv: list | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="AoT compile + agent query end-to-end demo.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--repo",
        default=_default_repo(),
        help="Path to the repo to compile + query (default: the codenib repo).",
    )
    p.add_argument(
        "--query",
        default="Where is the BM25 index implementation?",
        help="Prompt to send to the agent in Phase 2.",
    )
    p.add_argument(
        "--cache-dir",
        default=None,
        help="Where to write the manifest + index artifacts. "
        "Defaults to <repo>/.codenib_cache.",
    )
    p.add_argument(
        "--index-types",
        nargs="+",
        default=["bm25"],
        choices=["bm25", "vector", "symbol_graph"],
        help="Which indexes to build in Phase 1.",
    )
    p.add_argument(
        "--languages",
        nargs="+",
        default=["python"],
        help="Languages to chunk during Phase 1.",
    )
    p.add_argument(
        "--allowed-skills",
        nargs="+",
        default=["bm25_search"],
        help="Skills the agent may use in Phase 2.",
    )
    p.add_argument(
        "--primary-language",
        default="python",
        help="Primary language for CAR scenario classification.",
    )
    p.add_argument(
        "--model",
        default="vertex_ai/gemini-2.5-flash",
        help="LiteLLM model id for the Phase 2 agent loop.",
    )
    p.add_argument(
        "--max-turns",
        type=int,
        default=4,
        help="Cap on agent loop iterations.",
    )
    p.add_argument(
        "--no-llm",
        action="store_true",
        help="Run Phase 1 only — no LLM credentials needed. Useful for "
        "smoke-testing the compile path in CI.",
    )
    return p.parse_args(argv)


def main(argv: list | None = None) -> int:
    args = parse_args(argv)

    cache_dir = args.cache_dir or str(repo_index_dir(args.repo))

    # Phase 1 always runs.
    manifest = phase_one_compile(
        args.repo,
        index_types=args.index_types,
        languages=args.languages,
        cache_dir=cache_dir,
    )

    if args.no_llm:
        print("\n[Phase 2] Skipped (--no-llm).")
        return 0

    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") and args.model.startswith(
        "vertex_ai/"
    ):
        print(
            "\n[Phase 2] Skipped — GOOGLE_APPLICATION_CREDENTIALS not set "
            "and --model is a Vertex AI model. Export the SA JSON path or "
            "pass --no-llm to silence this notice.",
            file=sys.stderr,
        )
        return 0

    phase_two_query(
        manifest,
        prompt=args.query,
        model=args.model,
        allowed_skills=args.allowed_skills,
        primary_language=args.primary_language,
        max_turns=args.max_turns,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
