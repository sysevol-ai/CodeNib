#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Drive codeminer-synthesis v2 (multi-repo) generation from the v2 plan.

Consumes the plan from ``build_synthesis_v2_plan`` (25 repos, 5/language, one
graph-richest instance each, 20 queries/repo) and, per instance, invokes
``generate_codeminer_synthesis_config.py`` with that repo's per-category counts.
Resumable (skips an instance whose per-config seed/output already exists). Then
prints the ``rebuild_codeminer_synthesis_dataset.py`` assembly command.

Generation is heavy (repo clone + LSIndexer graph build + Opus curator/judge per
query via the Claude Agent SDK), so run it where that toolchain + auth live.
``--dry-run`` prints the exact commands without spending anything — use it to
review the plan before committing to the full run.

Usage::

    # review the commands (no cost):
    python scripts/generate_synthesis_v2.py --output-dir gen/v2 --dry-run
    # generate (heavy):
    python scripts/generate_synthesis_v2.py --output-dir gen/v2 \
        --cache-dir .cache/synth --repo-cache-dir .cache/repos
    # then assemble (command is printed at the end)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.build_synthesis_v2_plan import build_plan  # noqa: E402


def _counts_arg(counts: dict) -> str:
    return ",".join(f"{k}={v}" for k, v in counts.items() if v)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--output-dir", required=True, type=Path, help="generation root")
    p.add_argument("--prebuilt-dir", default="/mnt/data/codeminer")
    p.add_argument("--repos-per-lang", type=int, default=5)
    p.add_argument("--model-name", default="opus")
    p.add_argument("--judge-model", default="opus")
    p.add_argument("--cache-dir", default="")
    p.add_argument("--repo-cache-dir", default="")
    p.add_argument("--dry-run", action="store_true", help="print commands, run nothing")
    args = p.parse_args(argv)

    plan = build_plan(args.prebuilt_dir, args.repos_per_lang)
    print(
        f"v2 generation: {len(plan)} repos, "
        f"{sum(e['total_queries'] for e in plan)} queries"
    )

    failures: List[str] = []
    for e in plan:
        config = e["config"]
        iid = e["instance_id"]
        gen_dir = args.output_dir / config / iid
        seed_marker = gen_dir / "behavioral_x20" / f"synthesized_queries_{iid}.json"
        if seed_marker.exists():
            print(f"skip {config}/{iid} (already generated)")
            continue
        cmd = [
            sys.executable,
            "scripts/generate_codeminer_synthesis_config.py",
            "--config",
            config,
            "--instance-id",
            iid,
            "--counts",
            _counts_arg(e["counts"]),
            "--output-dir",
            str(gen_dir),
            "--model-name",
            args.model_name,
            "--judge-model",
            args.judge_model,
        ]
        if args.cache_dir:
            cmd += ["--cache-dir", args.cache_dir]
        if args.repo_cache_dir:
            cmd += ["--repo-cache-dir", args.repo_cache_dir]
        print(f"\n# {config}/{iid} ({e['repo']}, graph={e['graph_symbols']})")
        print("  " + " ".join(cmd))
        if args.dry_run:
            continue
        try:
            subprocess.run(cmd, cwd=str(_PROJECT_ROOT), check=True)
        except subprocess.CalledProcessError as exc:
            print(f"  FAIL {iid}: {exc}", file=sys.stderr)
            failures.append(iid)

    # assembly command (per-config replacements -> parquet)
    print("\n# assemble (after generation):")
    reps = " ".join(
        f"--replacement {c}={args.output_dir}/{c}"
        for c in sorted({e["config"] for e in plan})
    )
    print(
        f"  {sys.executable} scripts/rebuild_codeminer_synthesis_dataset.py "
        f"{reps} --output-dir {args.output_dir}/dataset"
    )
    if failures:
        print(f"\n{len(failures)} instance(s) FAILED: {failures}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
