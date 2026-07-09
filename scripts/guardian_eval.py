#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Evaluate Repository Guardian replay output against post-t ground truth.

For each episode (one commit replayed by guardian_replay.py) the evaluator:

1. Reads the episode's report.json to get the commit SHA and findings.
2. Extracts the post-t ground truth: files changed in the next
   ``--horizon`` commits after that SHA in the repo's history.
3. Scores findings against the GT: precision@k, recall, lead-time.

Two modes:

    # Single-arm scoring
    python scripts/guardian_eval.py \\
        --episodes guardian_output/run/episodes/ \\
        --repo . --horizon 20 --top-k 10

    # Memory vs. memoryless ablation (M4)
    python scripts/guardian_eval.py \\
        --episodes guardian_output/memory/episodes/ \\
        --memoryless-episodes guardian_output/memoryless/episodes/ \\
        --repo . --horizon 20 --top-k 10
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Ground-truth extraction
# ---------------------------------------------------------------------------


def _git_commits_after(
    repo_path: str,
    commit_t: str,
    n: int,
) -> List[Tuple[str, List[str]]]:
    """Return up to n commits after commit_t as (sha, [files]) pairs, oldest first.

    Includes only .py source files (the primary signal in the prototype).
    """
    try:
        out = subprocess.check_output(
            ["git", "log", "--name-only", "--format=%H", f"-n{n}", f"{commit_t}..HEAD"],
            cwd=repo_path,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return []

    commits: List[Tuple[str, List[str]]] = []
    current_sha: Optional[str] = None
    current_files: List[str] = []

    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue  # git log emits blanks after each SHA and between commits; skip both
        if len(line) == 40 and all(c in "0123456789abcdef" for c in line):
            if current_sha:
                commits.append((current_sha, current_files))
            current_sha = line
            current_files = []
        else:
            current_files.append(line)

    if current_sha:
        commits.append((current_sha, current_files))

    # git log returns newest-first; reverse so index 1 = first commit after t.
    commits.reverse()
    return commits


def post_t_changed_files(
    repo_path: str,
    commit_t: str,
    horizon_commits: int = 20,
) -> Dict[str, int]:
    """Return {file: commit_index} for files changed in the next horizon_commits commits.

    commit_index is 1-based: 1 = the first commit after commit_t.
    Only the first appearance of each file is recorded (smallest commit_index).
    Returns an empty dict if commit_t is not found in the history or no future
    commits exist.
    """
    commits = _git_commits_after(repo_path, commit_t, horizon_commits)
    result: Dict[str, int] = {}
    for idx, (_, files) in enumerate(commits, start=1):
        for f in files:
            if f not in result:
                result[f] = idx
    return result


# ---------------------------------------------------------------------------
# Episode loading
# ---------------------------------------------------------------------------


def load_episode_report(episode_dir: str) -> Optional[dict]:
    """Load report.json from an episode directory; return None on error."""
    path = Path(episode_dir) / "report.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"[warn] cannot load {path}: {exc}", file=sys.stderr)
        return None


def load_episode_findings(episode_dir: str) -> List[dict]:
    """Load findings list from report.json."""
    report = load_episode_report(episode_dir)
    if report is None:
        return []
    return report.get("findings", [])


def _extract_target_file(finding: dict) -> Optional[str]:
    """Return the file path a finding targets, or None.

    Churn findings embed the path in the title as 'High-churn file: <path>'.
    Evidence rows also carry file paths; the first evidence file is used as
    a fallback for non-churn findings.
    """
    kind = finding.get("kind", "")
    title = finding.get("title", "")
    prefix = "High-churn file: "
    if kind == "churn" and title.startswith(prefix):
        return title[len(prefix):]
    # Fallback: first evidence file (covers drift findings with file-level evidence).
    evidence = finding.get("evidence") or []
    if evidence and isinstance(evidence[0], dict):
        return evidence[0].get("file") or None
    return None


# ---------------------------------------------------------------------------
# Per-episode scoring
# ---------------------------------------------------------------------------


def score_episode(
    episode_dir: str,
    repo_path: str,
    *,
    horizon_commits: int = 20,
    top_k: int = 10,
) -> Optional[dict]:
    """Compute metrics for one episode.

    Returns a dict with keys:
        commit, precision_at_k, recall, lead_time_commits,
        n_findings, n_gt_files, token_cost
    Returns None if the episode report cannot be loaded.
    """
    report = load_episode_report(episode_dir)
    if report is None:
        return None

    commit = report.get("commit", "")
    findings = report.get("findings", [])
    usage = report.get("llm_usage") or {}
    token_cost = usage.get("total_tokens", 0) if isinstance(usage, dict) else 0

    # Extract target files from top-k findings (already in hypothesis-rank order).
    finding_files: List[str] = []
    for f in findings[:top_k]:
        target = _extract_target_file(f)
        if target and target not in finding_files:
            finding_files.append(target)

    # Ground truth: files changed in the next horizon_commits after this commit.
    gt = post_t_changed_files(repo_path, commit, horizon_commits)
    gt_files = set(gt.keys())

    if not gt_files:
        # No future commits found (e.g. this is HEAD); metrics are undefined.
        return {
            "commit": commit,
            "precision_at_k": None,
            "recall": None,
            "lead_time_commits": None,
            "n_findings": len(findings),
            "n_gt_files": 0,
            "token_cost": token_cost,
        }

    hits = [f for f in finding_files if f in gt_files]
    k_denom = min(top_k, max(1, len(finding_files)))
    precision = len(hits) / k_denom
    recall = len(hits) / len(gt_files)

    # Lead-time: for each correctly predicted file, how many commits in advance
    # Guardian flagged it (commit_index 1 = flagged at the very next commit).
    # We report the *minimum* lead time (the earliest confirmed prediction).
    lead_times = [gt[f] for f in hits if f in gt]
    lead_time = min(lead_times) if lead_times else None

    return {
        "commit": commit,
        "precision_at_k": round(precision, 3),
        "recall": round(recall, 3),
        "lead_time_commits": lead_time,
        "n_findings": len(findings),
        "n_gt_files": len(gt_files),
        "token_cost": token_cost,
    }


# ---------------------------------------------------------------------------
# Multi-episode scoring
# ---------------------------------------------------------------------------


def _sorted_episode_dirs(episodes_dir: str) -> List[str]:
    """Return episode subdirectory paths sorted by episode number."""
    base = Path(episodes_dir)
    if not base.exists():
        return []
    dirs = [d for d in base.iterdir() if d.is_dir()]
    dirs.sort(key=lambda d: d.name)
    return [str(d) for d in dirs]


def score_arm(
    episodes_dir: str,
    repo_path: str,
    *,
    horizon_commits: int = 20,
    top_k: int = 10,
) -> List[dict]:
    """Score all episodes in an arm; return list of per-episode metric dicts."""
    results = []
    for ep_dir in _sorted_episode_dirs(episodes_dir):
        row = score_episode(ep_dir, repo_path, horizon_commits=horizon_commits, top_k=top_k)
        if row is not None:
            results.append(row)
    return results


def _mean(values: List[Optional[float]]) -> Optional[float]:
    nums = [v for v in values if v is not None]
    return round(sum(nums) / len(nums), 3) if nums else None


def print_single_arm(
    rows: List[dict],
    *,
    top_k: int,
    horizon_commits: int,
    label: str = "Arm",
) -> None:
    print(f"\n=== Repository Guardian Evaluation — {label} ===")
    print(f"    top_k={top_k}  horizon={horizon_commits} commits")
    print()
    header = f"{'Commit':<12}  {'P@k':>6}  {'Recall':>6}  {'Lead':>5}  {'Findings':>8}  {'GT files':>8}  {'Tokens':>8}"
    print(header)
    print("-" * len(header))
    for r in rows:
        p = f"{r['precision_at_k']:.3f}" if r["precision_at_k"] is not None else "  N/A"
        rc = f"{r['recall']:.3f}" if r["recall"] is not None else "  N/A"
        lt = str(r["lead_time_commits"]) if r["lead_time_commits"] is not None else "  —"
        print(
            f"{r['commit'][:12]:<12}  {p:>6}  {rc:>6}  {lt:>5}  "
            f"{r['n_findings']:>8}  {r['n_gt_files']:>8}  {r['token_cost']:>8}"
        )
    print("-" * len(header))
    mean_p = _mean([r["precision_at_k"] for r in rows])
    mean_r = _mean([r["recall"] for r in rows])
    mean_lt = _mean([r["lead_time_commits"] for r in rows])  # type: ignore[arg-type]
    total_tok = sum(r["token_cost"] for r in rows)
    mp = f"{mean_p:.3f}" if mean_p is not None else "  N/A"
    mr = f"{mean_r:.3f}" if mean_r is not None else "  N/A"
    ml = f"{mean_lt:.1f}" if mean_lt is not None else "  —"
    print(f"{'MEAN':<12}  {mp:>6}  {mr:>6}  {ml:>5}  {'':>8}  {'':>8}  {total_tok:>8}")


def compare_arms(
    memory_episodes_dir: str,
    memoryless_episodes_dir: str,
    repo_path: str,
    *,
    horizon_commits: int = 20,
    top_k: int = 10,
) -> None:
    """Print a paired memory-vs-memoryless ablation table."""
    mem_rows = score_arm(memory_episodes_dir, repo_path, horizon_commits=horizon_commits, top_k=top_k)
    ml_rows = score_arm(memoryless_episodes_dir, repo_path, horizon_commits=horizon_commits, top_k=top_k)

    # Match by commit SHA (both arms replay the same commit list).
    ml_by_commit = {r["commit"]: r for r in ml_rows}

    print(f"\n=== Repository Guardian Ablation: Memory vs. Memoryless ===")
    print(f"    top_k={top_k}  horizon={horizon_commits} commits")
    print()
    header = (
        f"{'Commit':<12}  {'Mem P@k':>7}  {'ML P@k':>7}  {'Δ P':>6}  "
        f"{'Mem R':>6}  {'ML R':>6}  {'ΔR':>5}  {'Lead(m)':>7}  {'Lead(ml)':>8}"
    )
    print(header)
    print("-" * len(header))

    delta_p_list: List[float] = []
    delta_r_list: List[float] = []

    for r_mem in mem_rows:
        r_ml = ml_by_commit.get(r_mem["commit"])
        mp = r_mem["precision_at_k"]
        mlp = r_ml["precision_at_k"] if r_ml else None
        mr = r_mem["recall"]
        mlr = r_ml["recall"] if r_ml else None
        dp = round(mp - mlp, 3) if mp is not None and mlp is not None else None
        dr = round(mr - mlr, 3) if mr is not None and mlr is not None else None
        lt_m = r_mem["lead_time_commits"]
        lt_ml = r_ml["lead_time_commits"] if r_ml else None

        if dp is not None:
            delta_p_list.append(dp)
        if dr is not None:
            delta_r_list.append(dr)

        def _fmt(v, decimals=3):
            return f"{v:.{decimals}f}" if v is not None else "  N/A"

        def _fmti(v):
            return str(v) if v is not None else "  —"

        print(
            f"{r_mem['commit'][:12]:<12}  "
            f"{_fmt(mp):>7}  {_fmt(mlp):>7}  {_fmt(dp):>6}  "
            f"{_fmt(mr):>6}  {_fmt(mlr):>6}  {_fmt(dr):>5}  "
            f"{_fmti(lt_m):>7}  {_fmti(lt_ml):>8}"
        )

    print("-" * len(header))
    mean_dp = _mean(delta_p_list)  # type: ignore[arg-type]
    mean_dr = _mean(delta_r_list)  # type: ignore[arg-type]
    print(
        f"{'MEAN Δ':<12}  {'':>7}  {'':>7}  "
        f"{(_fmt(mean_dp) if mean_dp is not None else '  N/A'):>6}  "
        f"{'':>6}  {'':>6}  "
        f"{(_fmt(mean_dr) if mean_dr is not None else '  N/A'):>5}  {'':>7}  {'':>8}"
    )
    print()
    if mean_dp is not None:
        direction = "memory > memoryless" if mean_dp > 0 else ("tie" if mean_dp == 0 else "memoryless > memory")
        print(f"  Headline: mean ΔP@{top_k} = {mean_dp:+.3f}  ({direction})")
        if mean_dr is not None:
            print(f"            mean ΔRecall  = {mean_dr:+.3f}")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Score Guardian replay output against post-t ground truth.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--episodes", required=True,
                   help="Path to episodes/ directory from guardian_replay.py.")
    p.add_argument("--memoryless-episodes", default=None,
                   help="Episodes dir for the memoryless arm; triggers ablation mode.")
    p.add_argument("--repo", required=True,
                   help="Path to the git repository (for post-t ground truth).")
    p.add_argument("--horizon", type=int, default=20,
                   help="Number of commits after each episode commit to use as GT.")
    p.add_argument("--top-k", type=int, default=10,
                   help="Number of top findings to score (precision@k).")
    return p


def main() -> None:
    args = _build_parser().parse_args()
    repo_path = os.path.abspath(args.repo)

    if args.memoryless_episodes:
        compare_arms(
            args.episodes,
            args.memoryless_episodes,
            repo_path,
            horizon_commits=args.horizon,
            top_k=args.top_k,
        )
    else:
        rows = score_arm(args.episodes, repo_path, horizon_commits=args.horizon, top_k=args.top_k)
        if not rows:
            print("No episodes found or all failed to load.", file=sys.stderr)
            sys.exit(1)
        print_single_arm(rows, top_k=args.top_k, horizon_commits=args.horizon)


if __name__ == "__main__":
    main()
