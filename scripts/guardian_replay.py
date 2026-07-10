#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Replay a sequence of commits through the Repository Guardian.

Provisions one git worktree per commit, runs a single Guardian cycle in that
worktree, accumulates cross-cycle memory, and writes per-cycle reports.

Usage
-----
    python scripts/guardian_replay.py \\
        --repo /path/to/repo.git \\
        --commits commits.txt \\
        --arm memory \\
        --out runs/target_memory/ \\
        --index-types bm25 \\
        --since "90 days ago"

``commits.txt`` is a plain-text file with one full commit SHA per line.
Blank lines and lines starting with ``#`` are ignored.

Each cycle emits:
    <out>/episodes/<NNNN>_<sha8>/report.md   ← rendered Markdown
    <out>/episodes/<NNNN>_<sha8>/report.json ← full JSON sidecar
    <out>/episodes/<NNNN>_<sha8>/guardian.log

Cross-cycle state accumulates under ``<out>/memory/``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Worktree helpers
# ---------------------------------------------------------------------------


def _assert_mirror_unchanged(mirror: str, before_sha: str) -> None:
    """Raise AssertionError if the mirror HEAD moved (non-modifying invariant)."""
    after = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=mirror, text=True
    ).strip()
    assert after == before_sha, (
        f"Non-modifying invariant violated: mirror HEAD changed "
        f"from {before_sha} to {after}"
    )


def provision_worktree(mirror: str, commit: str, wt_base: str) -> str:
    """Create a detached worktree at *commit* under *wt_base*.

    Returns the absolute path of the new worktree directory.
    """
    wt_name = commit[:8]
    wt_path = os.path.join(wt_base, wt_name)
    os.makedirs(wt_base, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "--detach", wt_path, commit],
        cwd=mirror,
        check=True,
        capture_output=True,
        text=True,
    )
    return os.path.abspath(wt_path)


# ---------------------------------------------------------------------------
# Container sandbox helpers
# ---------------------------------------------------------------------------


def provision_container(
    wt_path: str,
    memory_dir: str,
    ep_dir: str,
    cfg: "GuardianConfig",
    *,
    image: str = "guardian-runtime:latest",
) -> subprocess.CompletedProcess:
    """Launch an ephemeral per-cycle container and block until it exits.

    Mounts:
      wt_path    → /repo    (read-only)
      memory_dir → /memory  (read-only; host writes persist state after exit)
      ep_dir     → /out     (read-write; container writes report.json/report.md)

    Network is disabled (``--network none``) so the container cannot reach
    external services.  Pass ``--use-llm`` only when a host model server is
    reachable via a future ``--add-host`` flag.
    """
    cmd = [
        "docker", "run", "--rm",
        "--network", "none",
        "-v", f"{os.path.abspath(wt_path)}:/repo:ro",
        "-v", f"{os.path.abspath(memory_dir)}:/memory:ro",
        "-v", f"{os.path.abspath(ep_dir)}:/out:rw",
        image,
        "--repo", "/repo",
        "--out", "/out",
        "--arm", cfg.arm,
        "--index-types", ",".join(cfg.index_types),
        "--since", cfg.since,
    ]
    return subprocess.run(cmd, check=True, text=True, capture_output=True)


def teardown_worktree(mirror: str, wt_path: str) -> None:
    """Remove a worktree that was previously provisioned by :func:`provision_worktree`."""
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", wt_path],
            cwd=mirror,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        logger.warning("guardian_replay: worktree remove failed for %s: %s", wt_path, exc)


# ---------------------------------------------------------------------------
# Replay loop
# ---------------------------------------------------------------------------

def _read_commits(path: str) -> list[str]:
    commits = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            commits.append(line)
    return commits


def run_replay(args: argparse.Namespace) -> None:
    from codeminer.guardian.cycle import GuardianConfig, run_cycle
    from codeminer.guardian.report import render_markdown

    commits = _read_commits(args.commits)
    if not commits:
        logger.error("guardian_replay: commits file is empty — nothing to do")
        sys.exit(1)

    out_dir = Path(args.out)
    memory_dir = out_dir / "memory"
    wt_base = out_dir / "worktrees"
    episodes_dir = out_dir / "episodes"
    memory_dir.mkdir(parents=True, exist_ok=True)

    index_types = [t.strip() for t in args.index_types.split(",") if t.strip()]
    repo = os.path.abspath(args.repo)

    logger.info(
        "guardian_replay: replaying %d commit(s) from %s (arm=%s, sandbox=%s)",
        len(commits), repo, args.arm, args.sandbox,
    )

    # Capture mirror HEAD before any cycle so we can assert it never moves.
    mirror_head_before = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()

    for i, commit in enumerate(commits):
        short = commit[:8]
        ep_name = f"{i:04d}_{short}"
        ep_dir = episodes_dir / ep_name
        ep_dir.mkdir(parents=True, exist_ok=True)

        logger.info("guardian_replay: [%d/%d] commit %s", i + 1, len(commits), short)

        wt_path = provision_worktree(repo, commit, str(wt_base))
        try:
            if args.sandbox == "container":
                cfg = GuardianConfig(
                    repo_path=wt_path,
                    memory_dir=str(memory_dir),
                    arm=args.arm,
                    index_types=tuple(index_types),
                    since=args.since,
                )
                provision_container(
                    wt_path, str(memory_dir), str(ep_dir), cfg,
                    image=args.container_image,
                )
                logger.info(
                    "guardian_replay: cycle %d done (container) — output in %s",
                    i, ep_dir,
                )
            else:
                cfg = GuardianConfig(
                    repo_path=wt_path,
                    memory_dir=str(memory_dir),
                    arm=args.arm,
                    index_types=tuple(index_types),
                    since=args.since,
                    index_cache_dir=str(out_dir / "cache" / short),
                    episode_dir=str(ep_dir),
                    use_llm=args.use_llm,
                    llm_model=args.llm_model,
                    graph_snapshot=True,
                )
                report = run_cycle(cfg)

                (ep_dir / "report.md").write_text(
                    render_markdown(report), encoding="utf-8"
                )
                (ep_dir / "report.json").write_text(
                    json.dumps(report.to_dict(), indent=2, default=str),
                    encoding="utf-8",
                )
                logger.info(
                    "guardian_replay: cycle %d done — %d finding(s), written to %s",
                    i, len(report.findings), ep_dir,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "guardian_replay: cycle %d failed for commit %s: %s",
                i, short, exc, exc_info=True,
            )
        finally:
            teardown_worktree(repo, wt_path)

    _assert_mirror_unchanged(repo, mirror_head_before)

    logger.info("guardian_replay: replay complete — output in %s", out_dir)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Replay Guardian cycles over a sequence of commits.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--repo", required=True, help="Path to the git repository (or bare mirror).")
    p.add_argument("--commits", required=True,
                   help="Path to a file with one commit SHA per line, oldest first. "
                        "Use 'git log --reverse --format=%%H' to produce the correct order.")
    p.add_argument("--out", required=True, help="Output directory for episodes, memory, and cache.")
    p.add_argument("--arm", default="memory", choices=["memory", "memoryless"],
                   help="Memory arm: 'memory' accumulates cross-cycle state; 'memoryless' runs "
                        "the same code path but discards state after each cycle.")
    p.add_argument("--index-types", default="bm25",
                   help="Comma-separated index types to build per cycle (e.g. 'bm25,symbol_graph').")
    p.add_argument("--since", default="90 days ago",
                   help="Churn window passed to git log for hotspot detection.")
    p.add_argument("--sandbox", default="worktree", choices=["worktree", "container"],
                   help="Isolation mode. 'worktree' runs the cycle in-process in a git worktree. "
                        "'container' launches an ephemeral Docker container per cycle.")
    p.add_argument("--container-image", default="guardian-runtime:latest",
                   help="Docker image to use when --sandbox container is set. "
                        "Build with: docker build -t guardian-runtime:latest "
                        "-f codeminer/guardian/Dockerfile .")
    p.add_argument("--use-llm", action="store_true",
                   help="Enable LLM narrative generation for each cycle.")
    p.add_argument("--llm-model", default="vertex_ai/gemini-2.5-flash",
                   help="LiteLLM model string used when --use-llm is set.")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="Logging verbosity.")
    return p


def main() -> None:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    )
    run_replay(args)


if __name__ == "__main__":
    main()
