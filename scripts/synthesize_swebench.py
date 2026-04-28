#!/usr/bin/env python3
"""
Synthesize natural-language queries from collected SWE-bench instances.

python3 scripts/synthesize_swebench.py \
  --dataset swebench_lite \
  --split test \
  --instance-id "astropy__astropy-6938" \
  --model-name opus \
  --query-types behavioral \
  --allowed-tools "Read,Grep,Glob,Bash" \
  --behavioral-consensus-runs 1 \
  --output-dir ./synthesis_output \
  --cache-dir ~/.codeminer \
  --repo-cache-dir ~/.codeminer
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from codeminer.dataset.swebench import SwebenchDataset
from codeminer.dataset.swebench_multilingual import SwebenchMultilingualDataset
from codeminer.dataset.synthesize import ClaudeQuerySynthesizer
from codeminer.dataset.utils import QueryType
from codeminer.log_utils import get_logger

logger = get_logger(__name__)


def _dump_json(data: object, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def _load_json_list(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"Expected list in {path}, got {type(data).__name__}")
    return data


def _parse_query_types(values: List[str]) -> List[QueryType]:
    tokens: List[str] = []
    for value in values:
        tokens.extend(part.strip() for part in value.split(",") if part.strip())
    if not tokens:
        tokens = [QueryType.BEHAVIORAL.value]
    return [QueryType(token) for token in tokens]


def _extract_ground_truth(instance: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract pre-computed ground truth from a codeminer-base-dataset instance.

    Maps ``gt_*`` prefixed fields to the format expected by
    ``ClaudeQuerySynthesizer.synthesize_query(ground_truth=...)``.
    Returns ``None`` when no ground truth fields are present.
    """
    target_files = instance.get("gt_target_files") or []
    symbols_modified = instance.get("gt_symbols_modified") or []
    symbols_deleted = instance.get("gt_symbols_deleted") or []
    if not target_files and not symbols_modified and not symbols_deleted:
        return None
    return {
        "target_files": list(target_files),
        "symbols_modified": list(symbols_modified),
        "symbols_deleted": list(symbols_deleted),
        "symbols_added": [],
    }


def _to_compact_record(item: Dict[str, Any]) -> Dict[str, Any]:
    record = {
        "repo": item.get("repo"),
        "instance_id": item.get("instance_id"),
        "base_commit": item.get("base_commit"),
        "query": item.get("query"),
        "category": item.get("query_type") or item.get("difficulty"),
        "gt_symbols": item.get("target_symbols") or [],
        "gt_symbol_nodes": item.get("target_symbol_nodes") or [],
        "gt_files": item.get("target_files") or [],
        "query_id": item.get("query_id"),
    }
    if item.get("language_group"):
        record["language_group"] = item["language_group"]
    if "verification_passed" in item:
        record["verification_passed"] = item["verification_passed"]
    if "error" in item:
        record["error"] = item["error"]
    return record


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synthesize natural-language queries for sampled SWE-bench instances."
    )
    parser.add_argument(
        "--selected-instances",
        type=str,
        default=None,
        help="Path to selected_instances.json",
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default="swebench_lite",
        choices=[
            "swebench_lite",
            "swebench_verified",
            "swebench_multilingual",
            "codeminer_base",
        ],
        help="Dataset to use when loading directly (default: swebench_lite)",
    )
    parser.add_argument(
        "--split",
        type=str,
        default="test",
        help="Dataset split to use (default: test)",
    )
    parser.add_argument(
        "--filter-instance",
        type=str,
        default=".*",
        help="Regex pattern to filter instances (default: .* for all)",
    )
    parser.add_argument("--cache-dir", type=str, default=None)
    parser.add_argument("--repo-cache-dir", type=str, default=None)
    parser.add_argument("--output-dir", type=str, default=None)
    parser.add_argument(
        "--instance-id",
        type=str,
        default=None,
        help="Debug mode: synthesize only this instance_id.",
    )
    parser.add_argument(
        "--model-name",
        type=str,
        default="opus",
        help="Claude agent model name (e.g., sonnet, opus).",
    )
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument(
        "--sampling-seed",
        type=int,
        default=None,
        help="Optional random seed for deterministic block sampling.",
    )
    parser.add_argument(
        "--behavioral-consensus-runs",
        type=int,
        default=3,
        help="Number of behavioral generation passes for GT consensus voting.",
    )
    parser.add_argument(
        "--permission-mode",
        type=str,
        default="bypassPermissions",
        help="Claude agent permission mode.",
    )
    parser.add_argument(
        "--allowed-tools",
        type=str,
        default="Read,Grep,Glob,Bash",
        help="Comma-separated list of allowed tools.",
    )
    parser.add_argument(
        "--start",
        type=int,
        default=0,
        help="Start index of instances to synthesize (0-based, inclusive).",
    )
    parser.add_argument(
        "--end",
        type=int,
        default=None,
        help="End index of instances to synthesize (exclusive). Defaults to all.",
    )
    parser.add_argument(
        "--repeat-per-instance",
        type=int,
        default=1,
        help="Generate multiple outputs per instance for debugging.",
    )
    parser.add_argument(
        "--query-types",
        nargs="*",
        default=[QueryType.BEHAVIORAL.value],
        help=(
            "Query types to synthesize (space or comma separated): "
            "behavioral,module_hint,file_hint,symbol_hint,reasoning "
            "(default: behavioral)"
        ),
    )
    parser.add_argument(
        "--output-file",
        type=str,
        default="synthesized_queries.json",
        help="Output JSON filename under output-dir.",
    )
    parser.add_argument(
        "--print-sample",
        type=int,
        default=1,
        help="Print the first N synthesized items to stdout.",
    )
    parser.add_argument(
        "--num-queries",
        type=int,
        default=1,
        help="Number of queries to generate per instance.",
    )
    parser.add_argument(
        "--verification-mode",
        type=str,
        default="lenient",
        choices=["strict", "lenient", "none"],
        help=(
            "Post-consensus verification mode: "
            "strict (retry with alternate runs on failure), "
            "lenient (warn but keep, default), "
            "none (skip verification)."
        ),
    )
    parser.add_argument(
        "--max-pipeline-restarts",
        type=int,
        default=2,
        help=(
            "For behavioral synthesis, restart the full checkout/index/sample/"
            "generate/verify pipeline this many additional times before failing."
        ),
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.repeat_per_instance < 1:
        raise ValueError("--repeat-per-instance must be >= 1")
    if args.max_pipeline_restarts < 0:
        raise ValueError("--max-pipeline-restarts must be >= 0")

    output_dir = Path(args.output_dir).expanduser() if args.output_dir else Path(".")
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = (
        Path(args.cache_dir).expanduser()
        if args.cache_dir
        else Path.home() / ".codeminer"
    )

    # Load instances from file or dataset
    if args.selected_instances:
        selected_path = Path(args.selected_instances).expanduser()
        selected_instances = _load_json_list(selected_path)
        if args.instance_id:
            selected_instances = [
                row
                for row in selected_instances
                if row.get("instance_id") == args.instance_id
            ]
            if not selected_instances:
                raise ValueError(
                    f"instance_id {args.instance_id!r} not found in {selected_path}"
                )
    else:
        # Load directly from SWE-bench dataset
        dataset_name_map = {
            "swebench_lite": "princeton-nlp/SWE-bench_Lite",
            "swebench_verified": "princeton-nlp/SWE-bench_Verified",
            "swebench_multilingual": "SWE-bench/SWE-bench_Multilingual",
            "codeminer_base": "fishmingyu/codeminer-base-dataset",
        }
        dataset_name = dataset_name_map[args.dataset]
        filter_pattern = (
            f"^({args.instance_id})$" if args.instance_id else args.filter_instance
        )

        logger.info(
            "Loading instances from %s (split=%s, filter=%s)",
            dataset_name,
            args.split,
            filter_pattern,
        )

        dataset_cls = (
            SwebenchMultilingualDataset
            if args.dataset in ("swebench_multilingual", "codeminer_base")
            else SwebenchDataset
        )
        dataset_obj = dataset_cls(
            dataset=dataset_name,
            split=args.split,
            filter_instance=filter_pattern,
            root=str(cache_dir),
            repo_root=args.repo_cache_dir or str(cache_dir),
        )
        dataset_instances = dataset_obj.load()

        # Convert to list of dicts
        selected_instances = [dict(instance) for instance in dataset_instances]

        if len(selected_instances) == 0:
            raise ValueError(
                f"No instances found in {dataset_name} with filter={filter_pattern}"
            )

        logger.info("Loaded %d instance(s) from dataset", len(selected_instances))

    allowed_tools = [
        tool.strip() for tool in args.allowed_tools.split(",") if tool.strip()
    ]
    query_types = _parse_query_types(args.query_types)
    if QueryType.BEHAVIORAL in query_types and args.verification_mode == "none":
        logger.warning(
            "behavioral synthesis with --verification-mode none skips "
            "query-target alignment checks"
        )

    synth_inputs = selected_instances[args.start : args.end]
    missing_required = [
        row.get("instance_id", "unknown")
        for row in synth_inputs
        if not row.get("base_commit") or not row.get("repo")
    ]
    if missing_required:
        preview = ", ".join(missing_required[:5])
        raise ValueError(
            "selected_instances.json is missing required fields "
            "(base_commit/repo) for: "
            f"{preview} (total={len(missing_required)}). "
            "Please re-run scripts/collect_swebench.sh."
        )

    expanded_inputs: List[Dict[str, Any]] = []
    run_ids: List[int] = []
    for instance in synth_inputs:
        for run_id in range(1, args.repeat_per_instance + 1):
            expanded = dict(instance)
            expanded["synthesis_run_id"] = run_id
            expanded_inputs.append(expanded)
            run_ids.append(run_id)

    logger.info(
        "Synthesizing %d runs across %d instances, %d query types (repeat=%d).",
        len(expanded_inputs),
        len(synth_inputs),
        len(query_types),
        args.repeat_per_instance,
    )
    if args.instance_id and args.output_file == "synthesized_queries.json":
        output_file = f"synthesized_queries_{args.instance_id}.json"
    else:
        output_file = args.output_file
    output_path = output_dir / output_file

    synthesized: List[Dict[str, Any]] = []
    for query_type in query_types:
        synthesizer = ClaudeQuerySynthesizer(
            model=args.model_name,
            max_turns=args.max_turns,
            allowed_tools=allowed_tools,
            permission_mode=args.permission_mode,
            query_type=query_type,
            sampling_seed=args.sampling_seed,
            behavioral_consensus_runs=args.behavioral_consensus_runs,
            num_queries=args.num_queries,
            verification_mode=args.verification_mode,
        )
        for inst_idx, instance in enumerate(expanded_inputs):
            run_id = run_ids[inst_idx] if inst_idx < len(run_ids) else 1
            for qi in range(args.num_queries):
                result = None
                max_attempts = (
                    args.max_pipeline_restarts + 1
                    if query_type == QueryType.BEHAVIORAL
                    else 1
                )
                for attempt in range(max_attempts):
                    attempt_instance = dict(instance)
                    attempt_query_index = qi
                    if query_type == QueryType.BEHAVIORAL and attempt:
                        attempt_instance["synthesis_run_id"] = f"{run_id}-r{attempt}"
                        attempt_query_index = qi + attempt
                    try:
                        gt = _extract_ground_truth(instance)
                        result = synthesizer.synthesize_query(
                            attempt_instance,
                            repo_root=args.repo_cache_dir,
                            cache_dir=str(cache_dir),
                            ground_truth=gt,
                            query_index=attempt_query_index,
                        )
                        if (
                            query_type == QueryType.BEHAVIORAL
                            and args.verification_mode == "strict"
                            and result.get("verification_passed") is False
                        ):
                            raise ValueError("behavioral query failed verification")
                        break
                    except Exception as exc:
                        instance_id = instance.get("instance_id", "unknown")
                        if attempt + 1 < max_attempts:
                            logger.warning(
                                "Pipeline attempt %d/%d failed for %s (q%d): %s, "
                                "restarting",
                                attempt + 1,
                                max_attempts,
                                instance_id,
                                qi + 1,
                                exc,
                            )
                        else:
                            logger.error(
                                "Failed to synthesize query for %s (q%d) "
                                "after %d attempt(s): %s",
                                instance_id,
                                qi + 1,
                                max_attempts,
                                exc,
                                exc_info=True,
                            )
                            result = {
                                "instance_id": instance_id,
                                "repo": instance.get("repo"),
                                "base_commit": instance.get("base_commit"),
                                "error": str(exc),
                            }

                result["language_group"] = instance.get("language_group")

                if args.repeat_per_instance > 1:
                    result["run_id"] = run_id
                    if "query_id" in result:
                        result["query_id"] = f"{result['query_id']}_run{run_id}"

                synthesized.append(_to_compact_record(result))
                _dump_json(synthesized, output_path)
                logger.info(
                    "Saved %d result(s) so far to %s", len(synthesized), output_path
                )

    if args.print_sample:
        print(
            json.dumps(synthesized[: args.print_sample], indent=2, ensure_ascii=False)
        )


if __name__ == "__main__":
    main()
