# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Build SCIP graph indexes for multiple benchmark sources.

Supported sources:
- swebench
- swebench_multilingual
- locbench
- sampled_csv (for sampled instance lists like selected.csv/selected_instances.csv)
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codeminer.compiler.snapshot_store import ArtifactProfile  # noqa: E402
from codeminer.compiler.snapshot_store import SnapshotArtifactStore, SourceSnapshot
from codeminer.dataset.locbench import LocbenchDataset  # noqa: E402
from codeminer.dataset.swebench import SwebenchDataset  # noqa: E402
from codeminer.dataset.swebench_multilingual import (  # noqa: E402
    SwebenchMultilingualDataset,
)
from codeminer.languages import language_capability_rows  # noqa: E402
from codeminer.log_utils import get_logger  # noqa: E402
from codeminer.ls_router import LSIndexer  # noqa: E402
from codeminer.profiler import Profiler  # noqa: E402

logger = get_logger(__name__)

DEFAULT_MULTILINGUAL_REPO_LANG_CSV = (
    Path(__file__).resolve().parents[1]
    / "codeminer"
    / "dataset"
    / "collect"
    / "data"
    / "swebench_multilingual_repos.csv"
)
GRAPH_LANGUAGES = sorted(
    row.key for row in language_capability_rows() if row.graph_backend is not None
)


@dataclass
class IndexTask:
    instance: Dict[str, Any]
    dataset_obj: Any
    language: str
    source_kind: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create SCIP graph indexes for benchmark instances.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--source",
        type=str,
        choices=["swebench", "swebench_multilingual", "locbench", "sampled_csv"],
        default="swebench",
        help="Source type for instances to index.",
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default="princeton-nlp/SWE-bench_Verified",
        help="Dataset name used when source is swebench.",
    )
    parser.add_argument(
        "--multilingual-dataset",
        type=str,
        default="SWE-bench/SWE-bench_Multilingual",
        help="Dataset name used when source is swebench_multilingual.",
    )
    parser.add_argument(
        "--locbench-dataset",
        type=str,
        default="czlll/Loc-Bench_V1",
        help="Dataset name used when source is locbench.",
    )
    parser.add_argument(
        "--sampled-csv",
        type=str,
        default=None,
        help="CSV file for sampled_csv source (e.g., selected.csv/selected_instances.csv).",
    )
    parser.add_argument(
        "--sampled-python-dataset",
        type=str,
        default="princeton-nlp/SWE-bench_Verified",
        help="Python backing dataset used to enrich sampled CSV rows if needed.",
    )
    parser.add_argument(
        "--sampled-multilingual-dataset",
        type=str,
        default="SWE-bench/SWE-bench_Multilingual",
        help="Multilingual backing dataset used to enrich sampled CSV rows if needed.",
    )
    parser.add_argument(
        "--sampled-locbench-dataset",
        type=str,
        default=None,
        help="Optional LocBench backing dataset to enrich sampled CSV rows.",
    )
    parser.add_argument(
        "--split", type=str, default="test", help="Dataset split to use."
    )
    parser.add_argument(
        "--filter-instance",
        type=str,
        default=".*",
        help="Regex pattern to filter instance IDs.",
    )
    parser.add_argument(
        "--output-path",
        type=str,
        default=None,
        help="Path to store generated graph indexes.",
    )
    parser.add_argument(
        "--exclude-patterns",
        type=str,
        nargs="+",
        default=["test*"],
        help="Patterns to exclude from indexing.",
    )
    parser.add_argument(
        "--skip-level",
        type=str,
        choices=["graph", "decode", "raw", "none"],
        default="graph",
        help="Cache/skip level for indexing pipeline.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force full re-index (equivalent to skip-level=none).",
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default="~/.codeminer",
        help="Cache directory for datasets.",
    )
    parser.add_argument(
        "--repo-cache-dir",
        type=str,
        default=None,
        help="Directory for checked-out repositories (defaults to cache-dir).",
    )
    parser.add_argument(
        "--profile-dir",
        type=str,
        default=None,
        help="Directory to store profiler summaries (default: <output-path>/profile_log).",
    )
    parser.add_argument(
        "--language",
        type=str,
        default="auto",
        choices=["auto", *GRAPH_LANGUAGES, "js", "ts"],
        help="SCIP index language. Use auto to infer per instance.",
    )
    parser.add_argument(
        "--default-language",
        type=str,
        default="python",
        choices=GRAPH_LANGUAGES,
        help="Fallback language when auto inference is inconclusive.",
    )
    parser.add_argument(
        "--artifact-layout",
        choices=["instance", "snapshot"],
        default="instance",
        help=(
            "Store each instance separately or bind it to a shared, "
            "content-addressed repository snapshot."
        ),
    )
    parser.add_argument(
        "--artifact-profile",
        default="benchmark-v1",
        help="Compatibility profile name used by snapshot-addressed artifacts.",
    )
    parser.add_argument(
        "--multilingual-repo-language-csv",
        type=str,
        default=str(DEFAULT_MULTILINGUAL_REPO_LANG_CSV),
        help="Repo->language CSV used for multilingual language inference.",
    )
    return parser.parse_args()


def _dataset_kwargs(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "split": args.split,
        "filter_instance": args.filter_instance,
        "root": args.cache_dir,
        "repo_root": args.repo_cache_dir or args.cache_dir,
        "log": True,
    }


def _map_language_label(label: Optional[str], default_language: str) -> str:
    if not label:
        return default_language
    text = label.lower()
    if "rust" in text:
        return "rust"
    if text == "go" or "golang" in text:
        return "go"
    if "javascript" in text or "typescript" in text or text == "ts" or text == "js":
        return "typescript"
    if "c++" in text or text == "cpp" or text == "c":
        return "cpp"
    if "c#" in text or "csharp" in text:
        return "csharp"
    if "java" in text:
        return "java"
    if "kotlin" in text:
        return "kotlin"
    if "ruby" in text:
        return "ruby"
    if "php" in text:
        return "php"
    if "scala" in text:
        return "scala"
    if "python" in text:
        return "python"
    return default_language


def _profile_languages(language: str) -> List[str]:
    if language in {"js", "ts", "javascript", "typescript"}:
        return ["javascript", "typescript"]
    return [language]


def _load_repo_language_map(csv_path: Path) -> Dict[str, str]:
    if not csv_path.exists():
        return {}
    mapping: Dict[str, str] = {}
    with csv_path.open("r", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            repo = row.get("repo") or row.get("Repository")
            lang = row.get("language") or row.get("Language")
            if repo and lang:
                mapping[repo] = lang
    return mapping


def _normalize_rows(rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [dict(r) for r in rows]


def _build_dataset(source: str, args: argparse.Namespace):
    kwargs = _dataset_kwargs(args)
    if source == "swebench":
        return SwebenchDataset(dataset=args.dataset, **kwargs)
    if source == "swebench_multilingual":
        return SwebenchMultilingualDataset(dataset=args.multilingual_dataset, **kwargs)
    if source == "locbench":
        return LocbenchDataset(dataset=args.locbench_dataset, **kwargs)
    raise ValueError(f"Unsupported source: {source}")


def _read_sampled_csv(csv_path: Path, pattern: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    regex = re.compile(pattern)
    with csv_path.open("r", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            iid = row.get("instance_id", "")
            if iid and regex.match(iid):
                rows.append(dict(row))
    return rows


def _load_instance_map(dataset_obj: Any) -> Dict[str, Dict[str, Any]]:
    records = _normalize_rows(dataset_obj.load())
    return {
        record["instance_id"]: record for record in records if record.get("instance_id")
    }


def _resolve_sampled_instances(
    args: argparse.Namespace,
    repo_language_map: Dict[str, str],
) -> List[IndexTask]:
    if not args.sampled_csv:
        raise ValueError("--sampled-csv is required when --source sampled_csv")

    sampled_rows = _read_sampled_csv(
        Path(args.sampled_csv).expanduser(), args.filter_instance
    )
    logger.info("Loaded %d sampled CSV rows", len(sampled_rows))

    kwargs = _dataset_kwargs(args)
    py_obj = SwebenchDataset(dataset=args.sampled_python_dataset, **kwargs)
    ml_obj = SwebenchMultilingualDataset(
        dataset=args.sampled_multilingual_dataset, **kwargs
    )
    py_map = _load_instance_map(py_obj)
    ml_map = _load_instance_map(ml_obj)

    loc_obj = None
    loc_map: Dict[str, Dict[str, Any]] = {}
    if args.sampled_locbench_dataset:
        loc_obj = LocbenchDataset(dataset=args.sampled_locbench_dataset, **kwargs)
        loc_map = _load_instance_map(loc_obj)

    tasks: List[IndexTask] = []
    missing: List[str] = []
    for row in sampled_rows:
        instance_id = row.get("instance_id")
        if not instance_id:
            continue

        lang_label = row.get("language_group") or row.get("language")
        preferred = _map_language_label(lang_label, default_language="python")

        source_obj = py_obj
        source_kind = "swebench"
        source_row = py_map.get(instance_id)

        if preferred != "python":
            source_obj = ml_obj
            source_kind = "swebench_multilingual"
            source_row = ml_map.get(instance_id)
        if source_row is None and instance_id in py_map:
            source_obj = py_obj
            source_kind = "swebench"
            source_row = py_map[instance_id]
        if source_row is None and instance_id in ml_map:
            source_obj = ml_obj
            source_kind = "swebench_multilingual"
            source_row = ml_map[instance_id]
        if source_row is None and instance_id in loc_map and loc_obj is not None:
            source_obj = loc_obj
            source_kind = "locbench"
            source_row = loc_map[instance_id]

        merged: Dict[str, Any]
        if source_row is None:
            merged = dict(row)
            if not merged.get("repo") or not merged.get("base_commit"):
                missing.append(instance_id)
                continue
        else:
            merged = {**source_row, **row}
            merged["repo"] = merged.get("repo") or source_row.get("repo")
            merged["base_commit"] = merged.get("base_commit") or source_row.get(
                "base_commit"
            )

        language = _infer_language(
            instance=merged,
            source_kind=source_kind,
            forced_language=args.language,
            default_language=args.default_language,
            repo_language_map=repo_language_map,
        )
        tasks.append(
            IndexTask(
                instance=merged,
                dataset_obj=source_obj,
                language=language,
                source_kind=source_kind,
            )
        )

    if missing:
        preview = ", ".join(missing[:5])
        raise ValueError(
            "Sampled CSV rows missing repo/base_commit and not found in backing datasets: "
            f"{preview} (total={len(missing)})"
        )
    return tasks


def _infer_language(
    *,
    instance: Mapping[str, Any],
    source_kind: str,
    forced_language: str,
    default_language: str,
    repo_language_map: Mapping[str, str],
) -> str:
    if forced_language != "auto":
        return forced_language
    if source_kind in {"swebench", "locbench"}:
        return "python"

    label = (
        instance.get("language_group")
        or instance.get("language")
        or repo_language_map.get(instance.get("repo", ""))
    )
    return _map_language_label(str(label) if label else None, default_language)


def _resolve_tasks(
    args: argparse.Namespace,
    repo_language_map: Dict[str, str],
) -> List[IndexTask]:
    if args.source == "sampled_csv":
        return _resolve_sampled_instances(args, repo_language_map)

    dataset_obj = _build_dataset(args.source, args)
    dataset_rows = _normalize_rows(dataset_obj.load())
    logger.info("Loaded %d instances from %s", len(dataset_rows), args.source)
    return [
        IndexTask(
            instance=instance,
            dataset_obj=dataset_obj,
            language=_infer_language(
                instance=instance,
                source_kind=args.source,
                forced_language=args.language,
                default_language=args.default_language,
                repo_language_map=repo_language_map,
            ),
            source_kind=args.source,
        )
        for instance in dataset_rows
    ]


def _ensure_required_fields(tasks: List[IndexTask]) -> None:
    missing: List[str] = []
    for task in tasks:
        instance = task.instance
        if not instance.get("repo") or not instance.get("base_commit"):
            missing.append(instance.get("instance_id", "unknown"))
    if missing:
        preview = ", ".join(missing[:5])
        raise ValueError(
            "Missing required fields repo/base_commit for instances: "
            f"{preview} (total={len(missing)})"
        )


def _write_profile(
    profile_output_dir: Path,
    *,
    instance: Mapping[str, Any],
    source_kind: str,
    language: str,
    profile_summary: Any,
) -> None:
    sections_payload = [
        {
            "label": label,
            "total": stats.total,
            "count": stats.count,
            "average": stats.average,
            "min": stats.safe_min,
            "max": stats.max_duration,
            "errors": stats.errors,
        }
        for label, stats in profile_summary
    ]
    profile_payload = {
        "instance_id": instance.get("instance_id"),
        "repo": instance.get("repo"),
        "base_commit": instance.get("base_commit"),
        "source": source_kind,
        "language": language,
        "total_duration": sum(section["total"] for section in sections_payload),
        "sections": sections_payload,
    }
    profile_file = (
        profile_output_dir
        / f"{str(instance.get('instance_id')).replace('/', '__')}.json"
    )
    profile_file.write_text(json.dumps(profile_payload, indent=2), encoding="utf-8")
    logger.info("Saved profiler results to %s", profile_file)


def main() -> None:
    args = parse_args()

    if args.output_path is None:
        args.output_path = str(Path.home() / ".codeminer")
    output_path = Path(args.output_path).expanduser()
    output_path.mkdir(parents=True, exist_ok=True)
    logger.info("Graph indexes will be stored in: %s", output_path)

    profile_output_dir = (
        Path(args.profile_dir).expanduser()
        if args.profile_dir
        else output_path / "profile_log"
    )
    profile_output_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Profiler summaries will be stored in: %s", profile_output_dir)

    repo_language_map = _load_repo_language_map(
        Path(args.multilingual_repo_language_csv).expanduser()
    )
    tasks = _resolve_tasks(args, repo_language_map)
    _ensure_required_fields(tasks)

    logger.info("Prepared %d indexing tasks from source=%s", len(tasks), args.source)
    effective_skip_level: Optional[str] = None
    if not args.force:
        effective_skip_level = None if args.skip_level == "none" else args.skip_level
    logger.info(
        "Index mode: force=%s, skip_level=%s",
        args.force,
        effective_skip_level if effective_skip_level is not None else "none",
    )
    snapshot_store = (
        SnapshotArtifactStore(output_path)
        if args.artifact_layout == "snapshot"
        else None
    )

    failures: List[str] = []
    for idx, task in enumerate(tasks):
        instance = task.instance
        instance_id = instance["instance_id"]
        repo = instance["repo"]
        base_commit = instance["base_commit"]

        logger.info("\n%s", "=" * 80)
        logger.info("Processing instance %d/%d: %s", idx + 1, len(tasks), instance_id)
        logger.info("Repository: %s", repo)
        logger.info("Base commit: %s", base_commit)
        logger.info("Source/language: %s / %s", task.source_kind, task.language)
        logger.info("%s", "=" * 80)

        try:
            task.dataset_obj.process_instance(
                instance, repo_root=args.repo_cache_dir or args.cache_dir
            )
            repo_path = task.dataset_obj.get_repo_path(
                instance, repo_root=args.repo_cache_dir or args.cache_dir
            )
            logger.info("Repository checked out at: %s", repo_path)

            if snapshot_store is not None:
                binding = snapshot_store.bind(
                    instance_id,
                    SourceSnapshot(repo=repo, commit=base_commit),
                    ArtifactProfile.create(
                        _profile_languages(task.language),
                        name=args.artifact_profile,
                    ),
                )
                repo_path = str(
                    snapshot_store.ensure_worktree(
                        binding,
                        source_repo=repo_path,
                        commit=base_commit,
                    )
                )
                instance_output_dir = binding.profile_dir
                logger.info(
                    "Snapshot artifact: snapshot=%s profile=%s "
                    "snapshot_hit=%s profile_hit=%s alias_hit=%s",
                    binding.snapshot_id,
                    binding.profile_id,
                    binding.snapshot_hit,
                    binding.profile_hit,
                    binding.alias_hit,
                )
            else:
                instance_output_dir = output_path / instance_id.replace("/", "__")
                instance_output_dir.mkdir(parents=True, exist_ok=True)

            profiler = Profiler(
                name=f"scip_indexer[{instance_id}]",
                logger=logger,
                emit_events=False,
                summary_level=logging.INFO,
            )
            indexer = LSIndexer(
                project_root=repo_path,
                output_dir=instance_output_dir,
                exclude_patterns=args.exclude_patterns,
                profiler=profiler,
                language=task.language,
            )

            logger.info("Starting graph indexing for %s", instance_id)
            graph = indexer.run_pipeline(
                skip_level=effective_skip_level,
                report_profile=False,
            )
            logger.info("Profiler summary for %s:", instance_id)
            profile_summary = profiler.report(reset=True)
            _write_profile(
                profile_output_dir,
                instance=instance,
                source_kind=task.source_kind,
                language=task.language,
                profile_summary=profile_summary,
            )

            if graph:
                logger.info("Successfully created graph index for %s", instance_id)
                logger.info("Graph saved to: %s", indexer.graph_file)
                logger.info(
                    "Graph stats: %d nodes, %d edges",
                    len(graph.graph.vs),
                    len(graph.graph.es),
                )
                quality = indexer.index_quality_report
                if quality is not None:
                    logger.info(
                        "Index quality: %s (%s)",
                        "passed" if quality["passed"] else "failed",
                        instance_output_dir / "index_quality.json",
                    )
            else:
                logger.error("Failed to create graph index for %s", instance_id)
                failures.append(instance_id)

        except Exception as exc:
            logger.error(
                "Error processing instance %s: %s", instance_id, exc, exc_info=True
            )
            failures.append(instance_id)
            continue

    logger.info("\n%s", "=" * 80)
    logger.info("Graph indexing complete!")
    logger.info("Processed %d instances", len(tasks))
    logger.info("Indexes stored in: %s", output_path)
    logger.info("%s", "=" * 80)
    if failures:
        raise SystemExit(
            "Graph indexing failed for "
            f"{len(failures)} instance(s): {', '.join(failures)}"
        )


if __name__ == "__main__":
    main()
