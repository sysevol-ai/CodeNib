#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""H2 downstream companion-agent pilot scaffold (W5.7a).

Three-arm A/B/C structure for evaluating whether Guardian findings help a
coding-agent solver close more issues:

    A — solo solver, no Guardian context
    B — solver + memoryless Guardian findings
    C — solver + memory Guardian findings

This module is a skeleton: ``MockSolver`` always returns passed=False so the
framework can be tested without a real coding agent.  Plug in a real solver
(e.g. a SWE-agent wrapper) during Week 4 by implementing the ``Solver`` protocol.

Usage::

    python scripts/guardian_h2_pilot.py \\
        --tasks tasks.json \\
        --memory-episodes guardian_output/ablation_memory/episodes/ \\
        --memoryless-episodes guardian_output/ablation_memoryless/episodes/ \\
        --solver mock
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Protocol, runtime_checkable

from codeminer.guardian.report import Finding


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Task:
    """One issue / pull-request the downstream solver is asked to close.

    Args:
        task_id: Unique identifier (e.g. SWE-bench instance ID).
        repo_path: Absolute path to the repository checkout.
        base_commit: The commit the solver starts from (the issue state).
        reference_commit: The human-written fix commit (used to determine
            which files are "in scope" for relevance filtering).
        test_command: Command to run to verify the fix (default: pytest).
    """

    task_id: str
    repo_path: str
    base_commit: str
    reference_commit: str
    test_command: str = "pytest tests/"


@dataclass
class ArmResult:
    """One solver run for a single (task, seed) pair.

    Args:
        task_id: Matches ``Task.task_id``.
        arm: ``"A"``, ``"B"``, or ``"C"``.
        passed: Whether the solver's patch made the reference tests pass.
        steps: Number of solver iterations / tool calls.
        tokens_used: LLM tokens consumed by the solver.
        seed: RNG seed for reproducibility.
    """

    task_id: str
    arm: str
    passed: bool
    steps: int
    tokens_used: int
    seed: int


# ---------------------------------------------------------------------------
# Solver protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Solver(Protocol):
    """Minimal interface for a coding-agent solver.

    Implement this to plug in a real solver (SWE-agent, OpenHands, etc.).
    """

    def solve(self, task: Task, context: str = "", *, seed: int = 0) -> ArmResult:
        """Attempt to fix ``task`` starting from ``task.base_commit``.

        Args:
            task: The task to solve.
            context: Optional Guardian findings injected as a hint prefix.
            seed: RNG seed for sampling.

        Returns:
            ``ArmResult`` describing whether the fix passed the reference tests.
        """
        ...  # pragma: no cover


@dataclass
class MockSolver:
    """Stub solver used during scaffolding and CI tests.

    Always returns ``passed=False`` — a deterministic result that lets the
    scaffold run end-to-end without a real coding agent.  Replace this with a
    real solver for the Week-4 pilot.
    """

    arm: str = "mock"

    def solve(self, task: Task, context: str = "", *, seed: int = 0) -> ArmResult:
        return ArmResult(
            task_id=task.task_id,
            arm=self.arm,
            passed=False,
            steps=1,
            tokens_used=0,
            seed=seed,
        )


# ---------------------------------------------------------------------------
# inject_findings
# ---------------------------------------------------------------------------


def _finding_target(finding: Finding) -> Optional[str]:
    """Extract the primary target file from a Finding."""
    prefix = "High-churn file: "
    if finding.kind == "churn" and finding.title.startswith(prefix):
        return finding.title[len(prefix):]
    if finding.evidence:
        return finding.evidence[0].file
    return None


def _touched_files_at(repo_path: str, base_commit: str, reference_commit: str) -> List[str]:
    """Return file paths changed between ``base_commit`` and ``reference_commit``."""
    try:
        out = subprocess.check_output(
            ["git", "diff", "--name-only", base_commit, reference_commit],
            cwd=repo_path,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return [line.strip() for line in out.splitlines() if line.strip()]
    except (subprocess.CalledProcessError, OSError):
        return []


def inject_findings(
    findings: List[Finding],
    task: Task,
    *,
    top_k: int = 5,
    touched_files: Optional[List[str]] = None,
) -> str:
    """Build a short context string with the top-k findings relevant to the task.

    Relevance: a finding is relevant when its target file appears in the set of
    files changed between ``task.base_commit`` and ``task.reference_commit``
    (i.e. it predicted a file the human fix actually touched).

    Args:
        findings: Guardian findings for this task (from memory or memoryless arm).
        task: The downstream coding task.
        top_k: Maximum number of findings to include.
        touched_files: Pre-computed list of files changed in the reference commit.
            When ``None``, computed from git.  Pass explicitly in tests to avoid
            needing a real git repository.

    Returns:
        A plain-text context string for the solver prompt, or ``""`` when no
        findings are relevant.
    """
    if touched_files is None:
        touched_files = _touched_files_at(
            task.repo_path, task.base_commit, task.reference_commit
        )
    touched_set = set(touched_files)

    relevant: List[Finding] = []
    for f in findings:
        if len(relevant) >= top_k:
            break
        target = _finding_target(f)
        if target and target in touched_set:
            relevant.append(f)

    if not relevant:
        return ""

    lines = ["Guardian findings relevant to this task:"]
    for f in relevant:
        target = _finding_target(f) or "?"
        hypothesis = f.hypothesis or f.detail or f.title
        verdict = f.verdict or "—"
        lines.append(f"- {target}: {hypothesis} (verdict: {verdict})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# run_arm
# ---------------------------------------------------------------------------


def run_arm(
    arm: str,
    tasks: List[Task],
    solver: Solver,
    findings_by_task: Dict[str, List[Finding]],
    *,
    top_k: int = 5,
    seeds: List[int] = (0, 1, 2),
) -> List[ArmResult]:
    """Run one arm across all tasks and seeds.

    Args:
        arm: ``"A"`` (no context), ``"B"`` (memoryless), or ``"C"`` (memory).
        tasks: List of tasks to solve.
        solver: Solver implementing the ``Solver`` protocol.
        findings_by_task: Map from task_id to List[Finding].  The caller supplies
            the appropriate findings for the arm (memoryless for B, memory for C).
        top_k: Top-k findings to inject (arms B and C only).
        seeds: RNG seeds; one ``ArmResult`` per (task, seed) pair.

    Returns:
        Flat list of ``ArmResult`` objects (len = tasks × seeds).
    """
    results: List[ArmResult] = []
    for task in tasks:
        context = ""
        if arm in ("B", "C"):
            task_findings = findings_by_task.get(task.task_id, [])
            context = inject_findings(task_findings, task, top_k=top_k)
        for seed in seeds:
            result = solver.solve(task, context=context, seed=seed)
            result.arm = arm
            results.append(result)
    return results


# ---------------------------------------------------------------------------
# compare_arms
# ---------------------------------------------------------------------------


def compare_arms(
    a_results: List[ArmResult],
    b_results: List[ArmResult],
    c_results: List[ArmResult],
) -> None:
    """Print C−A, C−B, B−A Δ pass-rates and fail→pass / pass→fail flip counts.

    Comparisons are made at the (task_id, seed) level; unmatched pairs are skipped.
    """

    def _index(results: List[ArmResult]) -> Dict[tuple, ArmResult]:
        return {(r.task_id, r.seed): r for r in results}

    a_idx = _index(a_results)
    b_idx = _index(b_results)
    c_idx = _index(c_results)

    def _compare(base_idx: dict, new_idx: dict):
        """Return (delta, fail_to_pass, pass_to_fail, n_pairs)."""
        keys = sorted(set(base_idx) & set(new_idx))
        n = len(keys)
        if not n:
            return None, 0, 0, 0
        base_pass = sum(base_idx[k].passed for k in keys)
        new_pass = sum(new_idx[k].passed for k in keys)
        delta = round((new_pass - base_pass) / n, 3)
        f2p = sum(1 for k in keys if not base_idx[k].passed and new_idx[k].passed)
        p2f = sum(1 for k in keys if base_idx[k].passed and not new_idx[k].passed)
        return delta, f2p, p2f, n

    ca_delta, ca_f2p, ca_p2f, n_ca = _compare(a_idx, c_idx)
    cb_delta, cb_f2p, cb_p2f, n_cb = _compare(b_idx, c_idx)
    ba_delta, ba_f2p, ba_p2f, n_ba = _compare(a_idx, b_idx)

    def _fmt(v):
        return f"{v:+.3f}" if v is not None else "  N/A"

    print()
    print("=== H2 Downstream Pilot — A/B/C Comparison ===")
    print(f"  C − A  (Guardian vs. solo):          {_fmt(ca_delta)}  (directional, n={n_ca})")
    print(f"  C − B  (memory vs. memoryless):      {_fmt(cb_delta)}")
    print(f"  B − A  (memoryless agent vs. solo):  {_fmt(ba_delta)}")
    print(f"  Fail→Pass (C helped):   {ca_f2p} / {n_ca}")
    print(f"  Pass→Fail (C harmed):   {ca_p2f} / {n_ca}")
    print()


# ---------------------------------------------------------------------------
# Episode-loading helpers
# ---------------------------------------------------------------------------


def _load_findings_from_episodes(episodes_dir: str) -> List[Finding]:
    """Load all findings from all episode report.json files as Finding objects."""
    _scripts_dir = os.path.dirname(os.path.abspath(__file__))
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
    from guardian_eval import _sorted_episode_dirs, load_episode_findings

    all_findings: List[Finding] = []
    for ep_dir in _sorted_episode_dirs(episodes_dir):
        for fd in load_episode_findings(ep_dir):
            all_findings.append(
                Finding(
                    kind=fd.get("kind", "churn"),
                    title=fd.get("title", ""),
                    detail=fd.get("detail", ""),
                    narrative=fd.get("narrative", ""),
                    hypothesis=fd.get("hypothesis", ""),
                    verdict=fd.get("verdict", ""),
                    evidence_test=fd.get("evidence_test", ""),
                    evidence_diff=fd.get("evidence_diff", ""),
                    reasoning_trace=fd.get("reasoning_trace", []),
                )
            )
    return all_findings


def _load_tasks(tasks_path: str) -> List[Task]:
    """Load tasks from a JSON file (list of task dicts)."""
    data = json.loads(Path(tasks_path).read_text(encoding="utf-8"))
    return [
        Task(
            task_id=t["task_id"],
            repo_path=t["repo_path"],
            base_commit=t["base_commit"],
            reference_commit=t["reference_commit"],
            test_command=t.get("test_command", "pytest tests/"),
        )
        for t in data
    ]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="H2 downstream pilot: run A/B/C arms and compare pass-rates.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--tasks", required=True,
                   help="Path to tasks.json (list of task dicts).")
    p.add_argument("--memory-episodes", default=None,
                   help="Episodes dir for the memory arm (arm C).")
    p.add_argument("--memoryless-episodes", default=None,
                   help="Episodes dir for the memoryless arm (arm B).")
    p.add_argument("--solver", choices=["mock"], default="mock",
                   help="Solver to use.  'mock' is a stub (always fails) for testing.")
    p.add_argument("--top-k", type=int, default=5,
                   help="Number of Guardian findings to inject per task.")
    p.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2],
                   help="RNG seeds (one run per seed per task).")
    return p


def main() -> None:
    args = _build_parser().parse_args()

    tasks = _load_tasks(args.tasks)
    if not tasks:
        print("No tasks found in tasks.json.", file=sys.stderr)
        sys.exit(1)

    if args.solver == "mock":
        solver: Solver = MockSolver()
    else:
        print(f"Unknown solver: {args.solver}", file=sys.stderr)
        sys.exit(1)

    # Load findings for arms B and C.
    memory_findings = (
        _load_findings_from_episodes(args.memory_episodes)
        if args.memory_episodes
        else []
    )
    memoryless_findings = (
        _load_findings_from_episodes(args.memoryless_episodes)
        if args.memoryless_episodes
        else []
    )

    # Skeleton matching: all findings are available for all tasks.
    findings_memory = {t.task_id: memory_findings for t in tasks}
    findings_memoryless = {t.task_id: memoryless_findings for t in tasks}

    seeds = args.seeds
    top_k = args.top_k

    print(f"Running arm A (solo, n={len(tasks)} tasks × {len(seeds)} seeds)…")
    a_results = run_arm("A", tasks, solver, {}, top_k=top_k, seeds=seeds)

    print(f"Running arm B (memoryless, findings={len(memoryless_findings)})…")
    b_results = run_arm("B", tasks, solver, findings_memoryless, top_k=top_k, seeds=seeds)

    print(f"Running arm C (memory, findings={len(memory_findings)})…")
    c_results = run_arm("C", tasks, solver, findings_memory, top_k=top_k, seeds=seeds)

    compare_arms(a_results, b_results, c_results)


if __name__ == "__main__":
    main()
