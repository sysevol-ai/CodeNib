# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Run the pinned CodeMiner Base native-LSP agent ablation."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from codeminer.agent.lsp_provider import StaticLSPProvider
from codeminer.eval.agent_runner.live_lsp_provider import LiveLSPReferenceProvider
from codeminer.graph.code_graph import CodeGraph
from codeminer.graph.incremental.lsp_client import LSPClient
from codeminer.llm.litellm_chat import LiteLLMChat
from codeminer.ls_index.index_quality import TRANSLATION_UNIT_SUFFIXES
from codeminer.scip_interface.lsp_occurrence_index import SCIPOccurrenceIndex

from .lsp_agent_study import (
    CODEMINER_LSP_ARM,
    DEFAULT_ARMS,
    LIVE_LSP_ARM,
    run_lsp_agent_study_arm,
    study_arm_order,
)
from .lsp_agent_study_analysis import summarize_lsp_agent_study
from .lsp_agent_study_artifacts import (
    requires_lsp_occurrence_index,
    static_native_backend_for_language,
)
from .lsp_agent_study_manifest import task_from_manifest_subject
from .lsp_readiness import wait_for_lsp_provider_readiness
from .lsp_replay_benchmark import generate_lsp_replay_requests

CellRunner = Callable[..., dict[str, Any]]
GraphLoader = Callable[[str | Path], Any]
LiveProviderFactory = Callable[..., Any]


def select_study_subjects(
    manifest: Mapping[str, Any],
    *,
    roles: Iterable[str] = (),
    instances: Iterable[str] = (),
    shard_count: int = 1,
    shard_index: int = 0,
    max_instances: Optional[int] = None,
) -> list[Mapping[str, Any]]:
    """Select subjects while keeping each repository in exactly one shard."""

    if shard_count < 1 or not 0 <= shard_index < shard_count:
        raise ValueError("shard_index must be in [0, shard_count)")
    role_filter = set(roles)
    instance_filter = set(instances)
    subjects = [
        row
        for row in manifest.get("subjects") or []
        if (not role_filter or str(row.get("role")) in role_filter)
        and (not instance_filter or str(row.get("instance_id")) in instance_filter)
    ]
    repos = sorted({str(row["repo"]) for row in subjects})
    selected_repos = {
        repo for index, repo in enumerate(repos) if index % shard_count == shard_index
    }
    subjects = sorted(
        (row for row in subjects if str(row["repo"]) in selected_repos),
        key=lambda row: str(row["instance_id"]),
    )
    if max_instances is not None:
        if max_instances < 1:
            raise ValueError("max_instances must be positive")
        subjects = subjects[:max_instances]
    if not subjects:
        raise ValueError("no study subjects matched the requested selection")
    return subjects


def planned_cell_ids(
    subjects: Sequence[Mapping[str, Any]],
    *,
    reps: int,
    randomization_seed: str,
) -> list[str]:
    """Return the exact cell IDs implied by a selected crossover."""

    cells = []
    for subject in subjects:
        instance_id = str(subject["instance_id"])
        for rep in range(1, reps + 1):
            for arm in study_arm_order(
                instance_id,
                rep,
                seed=randomization_seed,
            ):
                cells.append(_cell_id(instance_id, rep, arm))
    return cells


def preflight_lsp_agent_study(
    manifest: Mapping[str, Any],
    rows_by_id: Mapping[str, Mapping[str, Any]],
    subjects: Sequence[Mapping[str, Any]],
    *,
    artifact_root: str | Path,
    graph_loader: GraphLoader = CodeGraph.load_graph,
) -> dict[str, Any]:
    """Validate artifacts, task bindings, probes, and local LSP commands."""

    _validate_manifest(manifest)
    root = Path(artifact_root).expanduser().resolve()
    by_language: dict[str, int] = {}
    occurrence_count = 0
    probe_count = 0
    for subject in subjects:
        instance_id = str(subject["instance_id"])
        row = rows_by_id.get(instance_id)
        if row is None:
            raise ValueError(f"dataset row missing for {instance_id}")
        profile_dir = (root / instance_id).resolve(strict=True)
        task = task_from_manifest_subject(
            row,
            subject,
            repo_path=profile_dir / "repo",
        )
        graph = graph_loader(profile_dir / "graph.pkl")
        occurrence_index = _load_occurrence_index(profile_dir, task.language)
        if occurrence_index is not None:
            graph.lsp_occurrence_index = occurrence_index
            occurrence_count += len(occurrence_index.occurrences)
        if Path(graph.project_root).resolve() != Path(task.repo_path).resolve():
            raise ValueError(f"graph project_root mismatch for {instance_id}")
        if not LSPClient.check_lsp_available(task.language):
            raise RuntimeError(f"live LSP command unavailable for {task.language}")
        probes = generate_lsp_replay_requests(
            graph,
            project_root=task.repo_path,
            max_per_capability=2,
            file_suffixes=(
                tuple(TRANSLATION_UNIT_SUFFIXES) if task.language == "cpp" else None
            ),
        )
        if not probes:
            raise RuntimeError(f"no independent readiness probes for {instance_id}")
        probe_count += len(probes)
        by_language[task.language] = by_language.get(task.language, 0) + 1
    protocol = manifest["protocol"]
    return {
        "artifact_ready_count": len(subjects),
        "by_language": by_language,
        "manifest_sha256": manifest["manifest_sha256"],
        "lsp_occurrence_count": occurrence_count,
        "planned_cell_count": len(subjects) * int(protocol["reps"]) * len(DEFAULT_ARMS),
        "readiness_probe_count": probe_count,
        "repository_count": len({str(row["repo"]) for row in subjects}),
        "status": "ready",
        "subject_count": len(subjects),
    }


def run_lsp_agent_study_manifest(
    manifest: Mapping[str, Any],
    rows_by_id: Mapping[str, Mapping[str, Any]],
    subjects: Sequence[Mapping[str, Any]],
    *,
    artifact_root: str | Path,
    output_root: str | Path,
    llm: Any,
    reps: int,
    run_mode: str,
    retry_errors: bool = False,
    graph_loader: GraphLoader = CodeGraph.load_graph,
    live_provider_factory: LiveProviderFactory = LiveLSPReferenceProvider,
    cell_runner: CellRunner = run_lsp_agent_study_arm,
    idle_timeout_s: float = 120.0,
    readiness_minimum_reps: int = 2,
    readiness_maximum_reps: int = 8,
    readiness_poll_seconds: float = 1.0,
) -> dict[str, Any]:
    """Execute a resumable manifest selection and write one file per cell."""

    _validate_manifest(manifest)
    protocol = manifest["protocol"]
    if reps < 1:
        raise ValueError("reps must be positive")
    artifact_root = Path(artifact_root).expanduser().resolve()
    output_root = Path(output_root).expanduser().resolve()
    cells_dir = output_root / "cells"
    setup_dir = output_root / "setup"
    cells_dir.mkdir(parents=True, exist_ok=True)
    setup_dir.mkdir(parents=True, exist_ok=True)
    _ensure_run_identity(
        output_root / "run.json",
        {
            "dataset": manifest["dataset"],
            "manifest_sha256": manifest["manifest_sha256"],
            "model": getattr(llm, "model", protocol["model"]),
            "protocol": protocol,
            "run_mode": run_mode,
        },
    )

    randomization_seed = str(protocol["randomization_seed"])
    expected_ids = planned_cell_ids(
        subjects,
        reps=reps,
        randomization_seed=randomization_seed,
    )
    for subject in subjects:
        instance_id = str(subject["instance_id"])
        row = rows_by_id.get(instance_id)
        if row is None:
            raise ValueError(f"dataset row missing for {instance_id}")
        profile_dir = (artifact_root / instance_id).resolve(strict=True)
        task = task_from_manifest_subject(
            row,
            subject,
            repo_path=profile_dir / "repo",
        )
        graph = graph_loader(profile_dir / "graph.pkl")
        occurrence_index = _load_occurrence_index(profile_dir, task.language)
        if occurrence_index is not None:
            graph.lsp_occurrence_index = occurrence_index
        static_provider = StaticLSPProvider(
            graph,
            snapshot_id=str(subject["snapshot_id"]),
        )
        pending_live = any(
            not _cell_is_complete(
                cells_dir / f"{_cell_id(instance_id, rep, LIVE_LSP_ARM)}.json",
                retry_errors=retry_errors,
            )
            for rep in range(1, reps + 1)
        )
        live_provider = None
        live_setup: Optional[dict[str, Any]] = None
        live_setup_error: Optional[Exception] = None
        if pending_live:
            try:
                live_provider = live_provider_factory(
                    project_root=task.repo_path,
                    language=task.language,
                    skip_probe=True,
                )
                live_setup = _prepare_live_provider(
                    live_provider,
                    graph=graph,
                    project_root=task.repo_path,
                    idle_timeout_s=idle_timeout_s,
                    minimum_reps=readiness_minimum_reps,
                    maximum_reps=readiness_maximum_reps,
                    poll_seconds=readiness_poll_seconds,
                )
                _atomic_write_json(setup_dir / f"{instance_id}.json", live_setup)
            except Exception as exc:  # recorded as an experimental outcome
                live_setup_error = exc

        try:
            for rep in range(1, reps + 1):
                order = study_arm_order(instance_id, rep, seed=randomization_seed)
                for arm_order, arm in enumerate(order, 1):
                    path = cells_dir / f"{_cell_id(instance_id, rep, arm)}.json"
                    if _cell_is_complete(path, retry_errors=retry_errors):
                        continue
                    if arm == LIVE_LSP_ARM and live_setup_error is not None:
                        cell = _error_cell(
                            subject,
                            arm=arm,
                            rep=rep,
                            order=arm_order,
                            manifest_sha256=str(manifest["manifest_sha256"]),
                            model=str(getattr(llm, "model", protocol["model"])),
                            stage="live_readiness",
                            error=live_setup_error,
                        )
                    else:
                        provider = (
                            static_provider
                            if arm == CODEMINER_LSP_ARM
                            else live_provider if arm == LIVE_LSP_ARM else None
                        )
                        try:
                            cell = cell_runner(
                                task,
                                arm=arm,
                                graph=graph,
                                llm=llm,
                                provider=provider,
                                rep=rep,
                                order=arm_order,
                                max_turns=int(protocol["max_turns"]),
                                prompt_cache_enabled=bool(
                                    protocol["prompt_cache_enabled"]
                                ),
                            )
                            cell.update(
                                {
                                    "artifact_graph_sha256": subject["artifact"][
                                        "graph_sha256"
                                    ],
                                    "artifact_lsp_index_sha256": subject[
                                        "artifact"
                                    ].get("lsp_index_sha256"),
                                    "artifact_profile_id": subject["artifact"][
                                        "profile_id"
                                    ],
                                    "live_setup_sha256": (
                                        _sha256_json(live_setup)
                                        if arm == LIVE_LSP_ARM and live_setup
                                        else None
                                    ),
                                    "manifest_sha256": manifest["manifest_sha256"],
                                    "run_mode": run_mode,
                                    "status": "ok",
                                }
                            )
                        except Exception as exc:  # preserve failures in cells
                            cell = _error_cell(
                                subject,
                                arm=arm,
                                rep=rep,
                                order=arm_order,
                                manifest_sha256=str(manifest["manifest_sha256"]),
                                model=str(getattr(llm, "model", protocol["model"])),
                                stage="agent_cell",
                                error=exc,
                            )
                    _atomic_write_json(path, cell)
        finally:
            if live_provider is not None:
                live_provider.close()

    cells = _load_cells(cells_dir, expected_ids)
    error_count = sum(row.get("status") != "ok" for row in cells)
    summary = {
        "completed_cell_count": len(cells),
        "error_cell_count": error_count,
        "expected_cell_count": len(expected_ids),
        "manifest_sha256": manifest["manifest_sha256"],
        "run_mode": run_mode,
        "study": summarize_lsp_agent_study(cells),
    }
    summary["status"] = (
        "complete"
        if len(cells) == len(expected_ids) and error_count == 0
        else "incomplete"
    )
    _atomic_write_json(output_root / "summary.json", summary)
    return summary


def _prepare_live_provider(
    provider: Any,
    *,
    graph: Any,
    project_root: str | Path,
    idle_timeout_s: float,
    minimum_reps: int,
    maximum_reps: int,
    poll_seconds: float,
) -> dict[str, Any]:
    started = time.monotonic()
    provider.start()
    start_ms = (time.monotonic() - started) * 1000
    idle_started = time.monotonic()
    idle_grace_s = 10.0 if getattr(provider, "language", None) == "cpp" else 1.0
    if not provider.wait_until_idle(
        max_wait_s=idle_timeout_s,
        idle_grace_s=idle_grace_s,
    ):
        raise RuntimeError(f"live LSP did not become idle within {idle_timeout_s}s")
    idle_ms = (time.monotonic() - idle_started) * 1000
    probes = generate_lsp_replay_requests(
        graph,
        project_root=project_root,
        max_per_capability=2,
        file_suffixes=(
            tuple(TRANSLATION_UNIT_SUFFIXES)
            if getattr(provider, "language", None) == "cpp"
            else None
        ),
    )
    if not probes:
        raise RuntimeError("graph produced no independent live-readiness probes")
    static_provider = StaticLSPProvider(graph)
    readiness = wait_for_lsp_provider_readiness(
        probes,
        graph=graph,
        static_provider=static_provider,
        live_provider=provider,
        minimum_reps=minimum_reps,
        until_stable=True,
        max_reps=maximum_reps,
        poll_seconds=poll_seconds,
        min_live_nonempty_fraction=0.1,
        minimum_equivalent_count=0,
    )
    return {
        "effective_command": list(provider.effective_command or []),
        "equivalent_probe_count": readiness.equivalent_count,
        "idle_wait_ms": idle_ms,
        "idle_grace_s": idle_grace_s,
        "live_nonempty_probe_count": readiness.live_nonempty_count,
        "probe_count": len(probes),
        "provider_start_ms": start_ms,
        "readiness_reps": readiness.completed_reps,
        "readiness_rows": list(readiness.rows),
        "readiness_wall_ms": readiness.wall_ms,
        "stable": readiness.stable,
    }


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    declared = str(manifest.get("manifest_sha256") or "")
    payload = {
        key: value for key, value in manifest.items() if key != "manifest_sha256"
    }
    if not declared or _sha256_json(payload) != declared:
        raise ValueError("manifest SHA does not match its payload")
    schema_version = manifest.get("schema_version")
    if schema_version not in {2, 3}:
        raise ValueError("manifest must use schema version 2 or 3")
    protocol = manifest.get("protocol") or {}
    if tuple(protocol.get("arms") or ()) != DEFAULT_ARMS:
        raise ValueError("manifest does not declare the fixed three-arm protocol")
    if schema_version == 2:
        if protocol.get("static_native_backend") != "scip_occurrence_index_v1":
            raise ValueError("manifest does not pin the SCIP occurrence LSP backend")
    else:
        if protocol.get("static_native_backend") != "per_language_v1":
            raise ValueError("manifest does not pin per-language native backends")
        declared_backends = protocol.get("static_native_backend_by_language") or {}
        for subject in manifest.get("subjects") or []:
            language = str(subject.get("language") or "")
            expected = static_native_backend_for_language(language)
            if declared_backends.get(language) != expected:
                raise ValueError(f"manifest backend map mismatch for {language!r}")
            if subject.get("static_native_backend") != expected:
                raise ValueError(
                    f"manifest subject backend mismatch for {subject.get('instance_id')}"
                )
    if protocol.get("static_fallback_backend") != "symbol_graph_v1":
        raise ValueError("manifest does not pin the graph fallback backend")
    not_ready = [
        str(row.get("instance_id"))
        for row in manifest.get("subjects") or []
        if (row.get("artifact") or {}).get("status") != "ready"
    ]
    if not_ready:
        raise ValueError(f"manifest contains non-ready artifacts: {not_ready[:3]}")


def _load_occurrence_index(
    profile_dir: Path, language: str
) -> Optional[SCIPOccurrenceIndex]:
    path = profile_dir / "lsp_index.pkl"
    if path.is_file():
        return SCIPOccurrenceIndex.load(path)
    if requires_lsp_occurrence_index(language):
        raise ValueError(f"required LSP occurrence index missing at {path}")
    return None


def _ensure_run_identity(path: Path, identity: Mapping[str, Any]) -> None:
    if path.exists():
        current = json.loads(path.read_text(encoding="utf-8"))
        if current != dict(identity):
            raise ValueError(f"run identity mismatch at {path}")
        return
    _atomic_write_json(path, identity)


def _cell_is_complete(path: Path, *, retry_errors: bool) -> bool:
    if not path.is_file():
        return False
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    status = row.get("status")
    if status == "ok":
        return True
    return status == "error" and not retry_errors


def _error_cell(
    subject: Mapping[str, Any],
    *,
    arm: str,
    rep: int,
    order: int,
    manifest_sha256: str,
    model: str,
    stage: str,
    error: Exception,
) -> dict[str, Any]:
    return {
        "arm": arm,
        "base_commit": subject["base_commit"],
        "benchmark": "codeminer_base_lsp_agent_ablation",
        "error_message": str(error),
        "error_stage": stage,
        "error_type": type(error).__name__,
        "instance_id": subject["instance_id"],
        "language": subject["language"],
        "manifest_sha256": manifest_sha256,
        "model": model,
        "order": order,
        "rep": rep,
        "repo": subject["repo"],
        "role": subject["role"],
        "schema_version": 1,
        "snapshot_id": subject["snapshot_id"],
        "status": "error",
    }


def _load_cells(cells_dir: Path, expected_ids: Sequence[str]) -> list[dict[str, Any]]:
    cells = []
    for cell_id in expected_ids:
        path = cells_dir / f"{cell_id}.json"
        if not path.is_file():
            continue
        try:
            cells.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return cells


def _cell_id(instance_id: str, rep: int, arm: str) -> str:
    return f"{instance_id}__rep{rep}__{arm}"


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _sha256_json(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _load_dataset_rows(manifest: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    from datasets import load_dataset

    dataset_spec = manifest["dataset"]
    dataset = load_dataset(
        dataset_spec["name"],
        revision=dataset_spec["revision"],
        split=dataset_spec["split"],
    )
    expected_fingerprint = dataset_spec.get("fingerprint")
    actual_fingerprint = getattr(dataset, "_fingerprint", None)
    if expected_fingerprint and actual_fingerprint != expected_fingerprint:
        raise ValueError(
            "dataset fingerprint mismatch: "
            f"expected {expected_fingerprint}, found {actual_fingerprint}"
        )
    return {str(row["instance_id"]): dict(row) for row in dataset}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-json", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--output-root")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument(
        "--secondary-model",
        action="store_true",
        help="Run a separately reported non-primary model block.",
    )
    parser.add_argument("--model")
    parser.add_argument("--disable-thinking", action="store_true")
    parser.add_argument("--instance", action="append")
    parser.add_argument(
        "--role",
        action="append",
        choices=["development", "confirmatory", "language_extension"],
    )
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--max-instances", type=int)
    parser.add_argument("--reps", type=int)
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument("--num-retries", type=int, default=3)
    parser.add_argument("--vertex-project")
    parser.add_argument("--vertex-location")
    parser.add_argument("--idle-timeout-s", type=float, default=120.0)
    parser.add_argument("--readiness-minimum-reps", type=int, default=2)
    parser.add_argument("--readiness-maximum-reps", type=int, default=8)
    parser.add_argument("--readiness-poll-seconds", type=float, default=1.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = json.loads(Path(args.manifest_json).read_text(encoding="utf-8"))
        _validate_manifest(manifest)
        if args.max_instances is not None and not args.pilot:
            raise ValueError("--max-instances requires --pilot")
        primary_model = str(manifest["protocol"]["model"])
        selected_model = args.model or primary_model
        if selected_model != primary_model and not (args.pilot or args.secondary_model):
            raise ValueError(
                "formal primary runs cannot override the manifest model; "
                "use --secondary-model with a separate output root"
            )
        if args.secondary_model and selected_model == primary_model:
            raise ValueError("--secondary-model requires a non-primary --model")
        formal_reps = int(manifest["protocol"]["reps"])
        if args.reps is not None and not args.pilot and args.reps != formal_reps:
            raise ValueError("formal runs cannot override manifest repetitions")
        reps = (
            args.reps if args.reps is not None else (1 if args.pilot else formal_reps)
        )
        subjects = select_study_subjects(
            manifest,
            roles=args.role or (),
            instances=args.instance or (),
            shard_count=args.shard_count,
            shard_index=args.shard_index,
            max_instances=args.max_instances,
        )
        rows_by_id = _load_dataset_rows(manifest)
        if args.preflight:
            result = preflight_lsp_agent_study(
                manifest,
                rows_by_id,
                subjects,
                artifact_root=args.artifact_root,
            )
            sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
            return 0
        if not args.output_root:
            raise ValueError("--output-root is required unless --preflight is used")
        extra_kwargs: dict[str, Any] = {"num_retries": args.num_retries}
        if args.vertex_project:
            extra_kwargs["vertex_project"] = args.vertex_project
        if args.vertex_location:
            extra_kwargs["vertex_location"] = args.vertex_location
        if args.disable_thinking:
            extra_kwargs["extra_body"] = {
                "chat_template_kwargs": {"enable_thinking": False}
            }
        protocol = manifest["protocol"]
        llm = LiteLLMChat(
            model=selected_model,
            temperature=float(protocol["temperature"]),
            max_tokens=int(protocol["max_tokens"]),
            extra_kwargs=extra_kwargs,
        )
        summary = run_lsp_agent_study_manifest(
            manifest,
            rows_by_id,
            subjects,
            artifact_root=args.artifact_root,
            output_root=args.output_root,
            llm=llm,
            reps=int(reps),
            run_mode=(
                "pilot"
                if args.pilot
                else "secondary" if args.secondary_model else "formal"
            ),
            retry_errors=args.retry_errors,
            idle_timeout_s=args.idle_timeout_s,
            readiness_minimum_reps=args.readiness_minimum_reps,
            readiness_maximum_reps=args.readiness_maximum_reps,
            readiness_poll_seconds=args.readiness_poll_seconds,
        )
        sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        return 0 if summary["status"] == "complete" else 2
    except Exception as exc:  # noqa: BLE001 - CLI should fail cleanly.
        sys.stderr.write(f"codeminer-lsp-agent-study-run: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
