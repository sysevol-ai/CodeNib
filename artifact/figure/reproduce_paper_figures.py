#!/usr/bin/env python3
"""Rebuild every CodeMiner paper figure from an explicit artifact config."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
SOURCE_FILES = (
    "plot_style.py",
    "draw_eval_accuracy.py",
    "draw_pareto_rerank.py",
    "draw_graphrag_rerank.py",
    "draw_retrieval_ablation_suite.py",
    "draw_profile.py",
    "draw_query_profile.py",
    "draw_faiss_index_ablation.py",
    "draw_lsp_replay.py",
    "draw_lsp_replay_latency.py",
    "draw_materialized_trace.py",
    "draw_maintenance_lifecycle.py",
    "draw_serving_lifecycle_suite.py",
    "draw_agent_runtime.py",
    "draw_agent_runtime_schematic.py",
    "build_paper_claims.py",
)


@dataclass(frozen=True)
class FigureJob:
    name: str
    script: str
    arguments: tuple[str, ...]
    outputs: tuple[str, ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild CodeMiner paper figures and record provenance."
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="JSON config; relative input paths resolve from this file.",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace only the known outputs in a non-empty output directory.",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def path_identity(path: Path) -> dict[str, Any]:
    if path.is_file():
        return {
            "kind": "file",
            "files": 1,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    if not path.is_dir():
        raise FileNotFoundError(path)

    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        relative = child.relative_to(path).as_posix()
        child_sha = sha256_file(child)
        size = child.stat().st_size
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(child_sha.encode("ascii") + b"\0")
        digest.update(str(size).encode("ascii") + b"\n")
        file_count += 1
        total_bytes += size
    return {
        "kind": "directory",
        "files": file_count,
        "bytes": total_bytes,
        "sha256_tree": digest.hexdigest(),
    }


def resolve_path(config_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = config_dir / path
    return path.resolve()


def require_keys(mapping: dict[str, Any], keys: Iterable[str], label: str) -> None:
    missing = sorted(set(keys) - mapping.keys())
    if missing:
        raise ValueError(f"{label} is missing required keys: {', '.join(missing)}")


def load_config(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = json.loads(path.read_text())
    require_keys(raw, ("schema_version", "inputs", "protocol"), "config")
    if raw["schema_version"] != 5:
        raise ValueError(f"unsupported config schema: {raw['schema_version']!r}")

    inputs = raw["inputs"]
    require_keys(
        inputs,
        (
            "profile_dir",
            "query_dir",
            "model_scale_metadata",
            "model_cache_provenance",
            "eval_dir",
            "graph_result_dir",
            "dataset_stats",
            "faiss_results",
            "lsp_reports",
            "lifecycle_summary",
            "haiku_roots",
            "qwen9b_roots",
            "qwen27b_roots",
            "gemma12b_roots",
            "gemini25_roots",
        ),
        "inputs",
    )
    require_keys(
        inputs,
        (
            "graph_selection_results",
            "incremental_graph_results",
            "incremental_vector_results",
            "incremental_summary",
        ),
        "inputs",
    )
    config_dir = path.resolve().parent
    resolved: dict[str, Any] = {}
    for key, value in inputs.items():
        if isinstance(value, list):
            resolved[key] = [resolve_path(config_dir, item) for item in value]
        else:
            resolved[key] = resolve_path(config_dir, value)
    for key, value in resolved.items():
        paths = value if isinstance(value, list) else [value]
        if not paths:
            raise ValueError(f"inputs.{key} must not be empty")
        for item in paths:
            if not item.exists():
                raise FileNotFoundError(f"inputs.{key}: {item}")

    protocol = raw["protocol"]
    require_keys(
        protocol,
        (
            "graph_bootstrap_samples",
            "agent_bootstrap_samples",
            "agent_expected_pairs",
            "seed",
            "recall_guardrail",
            "accuracy_guardrail",
        ),
        "protocol",
    )
    return raw, resolved


def prefixed_arguments(flag: str, paths: Iterable[Path]) -> list[str]:
    result: list[str] = []
    for path in paths:
        result.extend((flag, str(path)))
    return result


def build_jobs(
    inputs: dict[str, Any], protocol: dict[str, Any], output_dir: Path
) -> list[FigureJob]:
    def prefix(name: str) -> str:
        return str(output_dir / name)

    graph_bootstrap = str(protocol["graph_bootstrap_samples"])
    agent_bootstrap = str(protocol["agent_bootstrap_samples"])
    seed = str(protocol["seed"])
    agent_roots = (
        inputs["haiku_roots"]
        + inputs["qwen9b_roots"]
        + inputs["qwen27b_roots"]
        + inputs["gemma12b_roots"]
        + inputs["gemini25_roots"]
    )
    agent_provenance_root = Path(
        os.path.commonpath([str(path.resolve()) for path in agent_roots])
    )
    graph_selection_arguments: tuple[str, ...] = ()
    if "graph_selection_results" in inputs:
        graph_selection_arguments = (
            "--graph-selection-results",
            str(inputs["graph_selection_results"]),
        )
    return [
        FigureJob(
            "figure_agent_runtime_schematic",
            "draw_agent_runtime_schematic.py",
            (
                "--output-prefix",
                prefix("agent_runtime_schematic"),
            ),
            (
                "agent_runtime_schematic.png",
                "agent_runtime_schematic.pdf",
            ),
        ),
        FigureJob(
            "audit_recall_curves",
            "draw_eval_accuracy.py",
            (
                "--eval-dir",
                str(inputs["eval_dir"]),
                "--output-prefix",
                prefix("eval_recall_at_k"),
            ),
            ("eval_recall_at_k.png", "eval_recall_at_k.pdf"),
        ),
        FigureJob(
            "figure_3_retrieval_pareto",
            "draw_pareto_rerank.py",
            (
                "--profile-dir",
                str(inputs["profile_dir"]),
                "--query-dir",
                str(inputs["query_dir"]),
                "--eval-dir",
                str(inputs["eval_dir"]),
                "--output-prefix",
                prefix("pareto_rerank"),
            ),
            ("pareto_rerank.png", "pareto_rerank.pdf"),
        ),
        FigureJob(
            "audit_graph_rerank_detail",
            "draw_graphrag_rerank.py",
            (
                "--result-dir",
                str(inputs["graph_result_dir"]),
                "--output-prefix",
                prefix("graphrag_vs_rerank"),
                "--partition",
                "test",
                "--bootstrap-samples",
                graph_bootstrap,
            ),
            (
                "graphrag_vs_rerank.json",
                "graphrag_vs_rerank.png",
                "graphrag_vs_rerank.pdf",
            ),
        ),
        FigureJob(
            "figure_5_materialization",
            "draw_profile.py",
            (
                "--data-dir",
                str(inputs["profile_dir"]),
                "--stats-file",
                str(inputs["dataset_stats"]),
                "--query-dir",
                str(inputs["query_dir"]),
                "--output-prefix",
                prefix("profile_l0_vs_l2"),
            ),
            ("profile_l0_vs_l2.png", "profile_l0_vs_l2.pdf"),
        ),
        FigureJob(
            "figure_5_query_latency",
            "draw_query_profile.py",
            (
                "--query-dir",
                str(inputs["query_dir"]),
                "--output-prefix",
                prefix("profile_query_time"),
            ),
            ("profile_query_time.png", "profile_query_time.pdf"),
        ),
        FigureJob(
            "audit_faiss_detail",
            "draw_faiss_index_ablation.py",
            (
                "--results",
                str(inputs["faiss_results"]),
                "--output-prefix",
                prefix("faiss_index_ablation"),
                "--accuracy-guardrail",
                str(protocol["accuracy_guardrail"]),
            ),
            ("faiss_index_ablation.png", "faiss_index_ablation.pdf"),
        ),
        FigureJob(
            "figure_4_retrieval_suite",
            "draw_retrieval_ablation_suite.py",
            graph_selection_arguments
            + (
                "--graph-result-dir",
                str(inputs["graph_result_dir"]),
                "--faiss-results",
                str(inputs["faiss_results"]),
                "--output-prefix",
                prefix("retrieval_ablation_suite"),
                "--partition",
                "test",
                "--bootstrap-samples",
                graph_bootstrap,
                "--accuracy-guardrail",
                str(protocol["accuracy_guardrail"]),
            ),
            (
                "retrieval_ablation_suite.png",
                "retrieval_ablation_suite.pdf",
            ),
        ),
        FigureJob(
            "audit_lsp_replay_with_ecdf",
            "draw_lsp_replay.py",
            (
                "--reports-dir",
                str(inputs["lsp_reports"]),
                "--output-prefix",
                prefix("lsp_replay_100"),
            ),
            ("lsp_replay_100.png", "lsp_replay_100.pdf"),
        ),
        FigureJob(
            "figure_6_lsp_latency",
            "draw_lsp_replay_latency.py",
            (
                "--reports-dir",
                str(inputs["lsp_reports"]),
                "--output-prefix",
                prefix("lsp_replay_latency"),
                "--panel-label",
                "",
            ),
            ("lsp_replay_latency.png", "lsp_replay_latency.pdf"),
        ),
        FigureJob(
            "audit_lifecycle_with_projection",
            "draw_materialized_trace.py",
            (
                "--summary",
                str(inputs["lifecycle_summary"]),
                "--output-prefix",
                prefix("materialized_trace"),
            ),
            ("materialized_trace.png", "materialized_trace.pdf"),
        ),
        FigureJob(
            "figure_9_maintenance_lifecycle",
            "draw_maintenance_lifecycle.py",
            (
                "--graph-results",
                str(inputs["incremental_graph_results"]),
                "--vector-results",
                str(inputs["incremental_vector_results"]),
                "--incremental-summary",
                str(inputs["incremental_summary"]),
                "--lifecycle-summary",
                str(inputs["lifecycle_summary"]),
                "--output-prefix",
                prefix("maintenance_lifecycle"),
            ),
            ("maintenance_lifecycle.png", "maintenance_lifecycle.pdf"),
        ),
        FigureJob(
            "audit_serving_lifecycle_suite",
            "draw_serving_lifecycle_suite.py",
            (
                "--reports-dir",
                str(inputs["lsp_reports"]),
                "--lifecycle-summary",
                str(inputs["lifecycle_summary"]),
                "--output-prefix",
                prefix("serving_lifecycle_suite"),
            ),
            (
                "serving_lifecycle_suite.png",
                "serving_lifecycle_suite.pdf",
            ),
        ),
        FigureJob(
            "figure_8_agent",
            "draw_agent_runtime.py",
            tuple(
                prefixed_arguments("--haiku-root", inputs["haiku_roots"])
                + prefixed_arguments("--qwen9b-root", inputs["qwen9b_roots"])
                + prefixed_arguments("--qwen27b-root", inputs["qwen27b_roots"])
                + prefixed_arguments("--gemma12b-root", inputs["gemma12b_roots"])
                + prefixed_arguments("--gemini25-root", inputs["gemini25_roots"])
                + [
                    "--output-prefix",
                    prefix("agent_runtime"),
                    "--breakdown-output-prefix",
                    prefix("agent_runtime_breakdown"),
                    "--expected-pairs",
                    str(protocol["agent_expected_pairs"]),
                    "--bootstrap-samples",
                    agent_bootstrap,
                    "--seed",
                    seed,
                    "--recall-guardrail",
                    str(protocol["recall_guardrail"]),
                    "--provenance-root",
                    str(agent_provenance_root),
                ]
            ),
            (
                "agent_runtime.json",
                "agent_runtime.png",
                "agent_runtime.pdf",
                "agent_runtime_breakdown.png",
                "agent_runtime_breakdown.pdf",
            ),
        ),
        FigureJob(
            "paper_claim_ledger",
            "build_paper_claims.py",
            (
                "--profile-dir",
                str(inputs["profile_dir"]),
                "--query-dir",
                str(inputs["query_dir"]),
                "--model-scale-metadata",
                str(inputs["model_scale_metadata"]),
                "--model-cache-provenance",
                str(inputs["model_cache_provenance"]),
                "--eval-dir",
                str(inputs["eval_dir"]),
                "--dataset-stats",
                str(inputs["dataset_stats"]),
                "--graph-result-dir",
                str(inputs["graph_result_dir"]),
                "--graph-selection-results",
                str(inputs["graph_selection_results"]),
                "--faiss-results",
                str(inputs["faiss_results"]),
                "--lsp-reports",
                str(inputs["lsp_reports"]),
                "--lifecycle-summary",
                str(inputs["lifecycle_summary"]),
                "--incremental-graph-results",
                str(inputs["incremental_graph_results"]),
                "--incremental-vector-results",
                str(inputs["incremental_vector_results"]),
                "--incremental-summary",
                str(inputs["incremental_summary"]),
                "--agent-summary",
                prefix("agent_runtime.json"),
                "--output",
                prefix("paper_claims.json"),
                "--graph-bootstrap-samples",
                graph_bootstrap,
                "--seed",
                seed,
                "--accuracy-guardrail",
                str(protocol["accuracy_guardrail"]),
            ),
            ("paper_claims.json",),
        ),
    ]


def ensure_output_dir(path: Path, jobs: Iterable[FigureJob], overwrite: bool) -> None:
    path.mkdir(parents=True, exist_ok=True)
    existing = list(path.iterdir())
    if existing and not overwrite:
        raise FileExistsError(
            f"output directory is not empty: {path}; pass --overwrite to replace "
            "known outputs"
        )
    if overwrite:
        known = {name for job in jobs for name in job.outputs}
        known.update({f"{job.name}.stdout.log" for job in jobs})
        known.add("reproduction_manifest.json")
        for name in known:
            candidate = path / name
            if candidate.exists():
                candidate.unlink()


def run_job(job: FigureJob, output_dir: Path) -> dict[str, Any]:
    command = [sys.executable, str(SCRIPT_DIR / job.script), *job.arguments]
    environment = os.environ.copy()
    environment.update(
        {
            "MPLBACKEND": "Agg",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
        }
    )
    completed = subprocess.run(
        command,
        cwd=SCRIPT_DIR,
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    log_path = output_dir / f"{job.name}.stdout.log"
    log_path.write_text(completed.stdout)
    print(completed.stdout, end="")
    if completed.returncode:
        raise subprocess.CalledProcessError(completed.returncode, command)

    outputs: dict[str, Any] = {}
    for name in job.outputs:
        path = output_dir / name
        if not path.is_file():
            raise FileNotFoundError(f"{job.name} did not create {path}")
        outputs[name] = path_identity(path)
    outputs[log_path.name] = path_identity(log_path)
    return {
        "script": job.script,
        "command": command,
        "outputs": outputs,
    }


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def main() -> None:
    args = parse_args()
    config_path = args.config.resolve()
    raw_config, inputs = load_config(config_path)
    output_dir = args.output_dir.resolve()
    jobs = build_jobs(inputs, raw_config["protocol"], output_dir)
    ensure_output_dir(output_dir, jobs, args.overwrite)

    input_identities: dict[str, Any] = {}
    for key, value in inputs.items():
        paths = value if isinstance(value, list) else [value]
        input_identities[key] = [
            {"path": str(path), **path_identity(path)} for path in paths
        ]

    source_identities = {
        name: path_identity(SCRIPT_DIR / name) for name in SOURCE_FILES
    }
    job_records = {job.name: run_job(job, output_dir) for job in jobs}
    manifest = {
        "schema_version": 1,
        "config_sha256": sha256_file(config_path),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "matplotlib": package_version("matplotlib"),
            "numpy": package_version("numpy"),
            "pandas": package_version("pandas"),
            "pillow": package_version("pillow"),
            "scipy": package_version("scipy"),
        },
        "protocol": raw_config["protocol"],
        "inputs": input_identities,
        "sources": source_identities,
        "jobs": job_records,
        "reproducibility_contract": {
            "bundle_mutation": (
                "Child processes disable Python bytecode writes so an extracted "
                "artifact remains checksum-clean when used as a read-only input."
            ),
            "deterministic_outputs": [
                name
                for job in jobs
                for name in job.outputs
                if name.endswith((".json", ".png"))
            ],
            "pdf_note": (
                "PDF CreationDate metadata can change across runs; compare page "
                "geometry, embedded fonts, or rasterized content instead of the "
                "raw PDF hash."
            ),
        },
    }
    manifest_path = output_dir / "reproduction_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"Saved: {manifest_path}")


if __name__ == "__main__":
    main()
