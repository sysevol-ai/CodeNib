#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Drive the agent-compile baseline sweep.

This is the Phase 0 / Phase 2 sweep harness from issue #133. It expands
the cartesian product ``{subsets} × {models} × {reps}`` over a partition
fitting pool (or held-out pool), invokes ``examples/skill_agent_eval.py``
once per cell, and explodes each run's report into per-cell JSON records
under ``<output_dir>/cells/`` so downstream aggregation has stable inputs.

Resume granularity is per *run* (one (subset, model, rep) triple). A run
is considered complete if its output JSON exists under
``<output_dir>/runs/``. To force a re-run, delete that file.

The script is intentionally a thin wrapper. The eval driver still owns
the per-instance loop, index building, agent execution, and metric
computation; we only orchestrate above it.

Usage::

    python scripts/agent_compile/run_sweep.py \\
        --config scripts/agent_compile/configs/phase0.yaml \\
        --partition data/agent_compile/partition.json \\
        --output-dir results/agent_compile/phase0
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_EVAL_SCRIPT = _PROJECT_ROOT / "examples" / "skill_agent_eval.py"


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


@dataclass
class SweepConfig:
    sweep_id: str
    subsets: Dict[str, List[str]]  # subset_id -> [skill_id, ...]
    models: List[str]  # litellm model names
    instances_source: str  # "fit" | "heldout"
    reps: int = 3
    max_turns: int = 20
    temperature: float = 0.0
    max_tokens: int = 4096
    dataset: str = "SWE-bench/SWE-bench_Multilingual"
    split: str = "test"
    languages: List[str] = field(default_factory=lambda: ["python"])
    metrics_k: List[int] = field(default_factory=lambda: [1, 3, 5, 10])
    embedding_model: str = "nomic-ai/CodeRankEmbed"
    embedding_dimension: int = 768
    index_cache_dir: Optional[str] = None
    eval_instances_path: Optional[str] = None  # GT-locate JSON
    extra_env: Dict[str, str] = field(default_factory=dict)
    gt_symbols_mode: str = "simplified"
    repo_cache_dir: Optional[str] = None

    @classmethod
    def from_yaml(cls, path: Path) -> "SweepConfig":
        with path.open("r", encoding="utf-8") as f:
            payload = yaml.safe_load(f) or {}
        if "subsets" not in payload or not isinstance(payload["subsets"], dict):
            raise ValueError(
                f"{path}: 'subsets' must be a mapping of subset_id -> [skills]"
            )
        if "models" not in payload or not isinstance(payload["models"], list):
            raise ValueError(f"{path}: 'models' must be a list of model names")
        if "sweep_id" not in payload:
            raise ValueError(f"{path}: 'sweep_id' is required")
        return cls(**payload)


# ---------------------------------------------------------------------------
# Cell / run records
# ---------------------------------------------------------------------------


@dataclass
class RunSpec:
    """One (subset, model, rep) invocation of the eval driver."""

    subset_id: str
    skills: List[str]
    model: str
    rep: int

    @property
    def run_id(self) -> str:
        return f"{self.subset_id}__{_slug(self.model)}__rep{self.rep}"


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")


# ---------------------------------------------------------------------------
# Eval-driver invocation
# ---------------------------------------------------------------------------


def _instances_filter_regex(instance_ids: Sequence[str]) -> str:
    """Build an exact-match regex over a list of instance IDs.

    ``examples/skill_agent_eval.py --filter-instance`` is matched as a
    regex against ``instance_id``. We anchor each ID with ``^...$`` so
    accidental substring matches don't pick up extra instances.
    """
    return "^(" + "|".join(re.escape(iid) for iid in instance_ids) + ")$"


def _build_eval_cmd(
    cfg: SweepConfig,
    spec: RunSpec,
    instance_ids: Sequence[str],
    result_path: Path,
) -> List[str]:
    cmd: List[str] = [
        sys.executable,
        str(_EVAL_SCRIPT),
        "--dataset",
        cfg.dataset,
        "--split",
        cfg.split,
        "--model",
        spec.model,
        "--temperature",
        str(cfg.temperature),
        "--max-tokens",
        str(cfg.max_tokens),
        "--max-turns",
        str(cfg.max_turns),
        "--filter-instance",
        _instances_filter_regex(instance_ids),
        "--skills",
        *spec.skills,
        "--metrics-k",
        *[str(k) for k in cfg.metrics_k],
        "--languages",
        *cfg.languages,
        "--embedding-model",
        cfg.embedding_model,
        "--embedding-dimension",
        str(cfg.embedding_dimension),
        "--result-path",
        str(result_path),
        "--gt-symbols",
        cfg.gt_symbols_mode,
    ]
    if cfg.index_cache_dir:
        cmd.extend(["--index-cache-dir", cfg.index_cache_dir])
    if cfg.eval_instances_path:
        cmd.extend(["--eval-instances", cfg.eval_instances_path])
    if cfg.repo_cache_dir:
        cmd.extend(["--repo-cache-dir", cfg.repo_cache_dir])
    return cmd


def _explode_run_to_cells(
    run_payload: Dict[str, Any],
    spec: RunSpec,
    cells_dir: Path,
) -> int:
    """Write one cell JSON per instance in the run's report.

    Returns the count of cells written.
    """
    cells_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for inst in run_payload.get("results", []):
        instance_id = inst.get("instance_id")
        if not instance_id:
            continue
        cell_id = f"{_slug(instance_id)}__{spec.subset_id}__{_slug(spec.model)}__rep{spec.rep}"
        cell_path = cells_dir / f"{cell_id}.json"
        loc = inst.get("loc_result") or {}
        usage = loc.get("usage") or {}
        token_usage = usage.get("token_usage") or {}
        record = {
            "cell_id": cell_id,
            "instance_id": instance_id,
            "subset_id": spec.subset_id,
            "skills": spec.skills,
            "model": spec.model,
            "rep": spec.rep,
            "success": inst.get("success", False),
            "metrics": inst.get("metrics", {}),
            "metrics_meaningful": inst.get("metrics_meaningful", False),
            "target_files": inst.get("target_files", []),
            "target_symbols": inst.get("target_symbols", []),
            "total_turns": usage.get("total_turns"),
            "total_duration_ms": usage.get("total_duration_ms"),
            "tool_call_count": usage.get("tool_call_count"),
            "prompt_tokens": token_usage.get("prompt_tokens"),
            "completion_tokens": token_usage.get("completion_tokens"),
            "total_tokens": token_usage.get("total_tokens"),
            "cost_usd": token_usage.get("cost_usd"),
            "elapsed_seconds": inst.get("elapsed_seconds"),
            "error": inst.get("error"),
            # Preserved verbatim for Phase 2's invocation + success-rate
            # histograms; Phase 0 ignores it.
            "execution_log": loc.get("execution_log", []),
        }
        with cell_path.open("w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)
        written += 1
    return written


# ---------------------------------------------------------------------------
# Sweep loop
# ---------------------------------------------------------------------------


def _runspecs(cfg: SweepConfig) -> List[RunSpec]:
    out: List[RunSpec] = []
    for subset_id, skills in cfg.subsets.items():
        for model in cfg.models:
            for rep in range(1, cfg.reps + 1):
                out.append(
                    RunSpec(
                        subset_id=subset_id,
                        skills=list(skills),
                        model=model,
                        rep=rep,
                    )
                )
    return out


def _load_partition_instances(partition_path: Path, source: str) -> List[str]:
    with partition_path.open("r", encoding="utf-8") as f:
        partition = json.load(f)
    if source not in ("fit", "heldout"):
        raise ValueError(f"instances_source must be 'fit' or 'heldout', got {source!r}")
    ids = partition.get(source) or []
    if not ids:
        raise RuntimeError(
            f"{partition_path} has no {source!r} instances — re-run partition.py"
        )
    return list(ids)


def run_sweep(
    cfg: SweepConfig,
    instance_ids: Sequence[str],
    output_dir: Path,
    *,
    dry_run: bool = False,
    resume: bool = True,
) -> Dict[str, Any]:
    runs_dir = output_dir / "runs"
    cells_dir = output_dir / "cells"
    runs_dir.mkdir(parents=True, exist_ok=True)
    cells_dir.mkdir(parents=True, exist_ok=True)

    specs = _runspecs(cfg)
    summary: Dict[str, Any] = {
        "sweep_id": cfg.sweep_id,
        "instance_count": len(instance_ids),
        "run_count": len(specs),
        "completed": [],
        "skipped": [],
        "failed": [],
    }

    env = dict(os.environ)
    env.update(cfg.extra_env)

    for spec in specs:
        run_path = runs_dir / f"{spec.run_id}.json"
        if resume and run_path.exists():
            summary["skipped"].append(spec.run_id)
            print(f"sweep: skip {spec.run_id} (resume; run file exists)")
            continue

        cmd = _build_eval_cmd(cfg, spec, instance_ids, run_path)
        print(f"sweep: launching {spec.run_id} " f"(n={len(instance_ids)} instances)")
        if dry_run:
            print("  cmd:", " ".join(cmd))
            summary["skipped"].append(spec.run_id)
            continue

        start = time.time()
        try:
            subprocess.run(cmd, check=True, env=env)
        except subprocess.CalledProcessError as exc:
            elapsed = time.time() - start
            print(
                f"sweep: FAIL {spec.run_id} after {elapsed:.0f}s "
                f"(returncode={exc.returncode})",
                file=sys.stderr,
            )
            summary["failed"].append(
                {"run_id": spec.run_id, "returncode": exc.returncode}
            )
            continue

        elapsed = time.time() - start
        if not run_path.exists():
            print(
                f"sweep: FAIL {spec.run_id} produced no output JSON at " f"{run_path}",
                file=sys.stderr,
            )
            summary["failed"].append({"run_id": spec.run_id, "reason": "no-output"})
            continue

        with run_path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        n_cells = _explode_run_to_cells(payload, spec, cells_dir)
        summary["completed"].append(
            {
                "run_id": spec.run_id,
                "elapsed_seconds": elapsed,
                "cells_written": n_cells,
            }
        )
        print(f"sweep: done {spec.run_id} in {elapsed:.0f}s, " f"wrote {n_cells} cells")

    summary_path = output_dir / "sweep_summary.json"
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Drive the agent-compile sweep over a fit / held-out pool.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--config",
        required=True,
        type=Path,
        help="Path to sweep YAML (subsets, models, reps, ...).",
    )
    parser.add_argument(
        "--partition",
        required=True,
        type=Path,
        help="Path to partition.json produced by partition.py.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Where to write run JSONs, cell JSONs, and sweep_summary.json.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned commands without invoking the eval driver.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Re-run every cell even if a run file already exists.",
    )
    args = parser.parse_args(argv)

    cfg = SweepConfig.from_yaml(args.config)
    instances = _load_partition_instances(args.partition, cfg.instances_source)

    summary = run_sweep(
        cfg,
        instances,
        args.output_dir,
        dry_run=args.dry_run,
        resume=not args.no_resume,
    )

    print(
        "sweep done: completed={c} skipped={s} failed={f} cells_dir={d}".format(
            c=len(summary["completed"]),
            s=len(summary["skipped"]),
            f=len(summary["failed"]),
            d=args.output_dir / "cells",
        )
    )
    return 0 if not summary["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
