# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
MCP server benchmark on the CodeNib base dataset.

Loads `fishmingyu/codenib-base-dataset` (test split) and runs semantic
search via the MCP server's `search_semantic` tool against pre-built
indexes under CODENIB_DATA/<instance_id>. Reports file-level and
symbol-level accuracy@k, broken down by language_group and
difficulty_level.
"""

import argparse
import asyncio
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Mapping

import datasets

from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.index.embedding.vector_store import CodeVectorStore
from codenib.mcp.context import ServerContext
from codenib.mcp.tools.search import search_semantic
from codenib.paths import prebuilt_data_dir

CODENIB_DATA = prebuilt_data_dir()
# Model + dim defaults: Salesforce/SweRankEmbed-Small has 100/100 prebuilt
# index coverage on ${CODENIB_PREBUILT_DIR} for this dataset, so it's the
# benchmark default. Override with --model.
DEFAULT_EMBEDDING_MODEL = "Salesforce/SweRankEmbed-Small"
DEFAULT_EMBEDDING_REVISION = "745d2a06103a66d3cfa600aa52fc0d3523010daa"
DEFAULT_DIMENSION = 768
DATASET_NAME = "fishmingyu/codenib-base-dataset"
DATASET_SPLIT = "test"

# language_group (from codenib-base schema) -> manifest `languages` list.
# Strings on the right match the keys understood by
# codenib/compiler/index_builders.py.
LANGUAGE_GROUP_MAP: Dict[str, List[str]] = {
    "Python": ["python"],
    "Go": ["go"],
    "C++/C": ["cpp"],
    "Rust": ["rust"],
    "TypeScript/JavaScript": ["typescript", "javascript"],
}


def _load_vector_from_local_admin_boundary(
    store: CodeVectorStore,
    path: str | Path,
    *,
    semantic_contract: Mapping[str, Any],
    evidence: str,
) -> None:
    """Authorize one prebuilt local benchmark view at the CLI boundary."""

    from codenib.index.embedding.artifact_integrity import (
        capture_authenticated_vector_view,
    )
    from codenib.native_index_authorization import (
        _mint_trusted_local_admin_authorization,
    )

    if not evidence:
        raise ValueError("local vector authorization evidence must be non-empty")
    with capture_authenticated_vector_view(path) as vector_view:
        authorization = _mint_trusted_local_admin_authorization(
            vector_view.ownership,
            view_type="vector",
            semantic_contract=semantic_contract,
            evidence=(
                evidence,
                "mcp-benchmark-cli-local-admin",
                "captured-vector-tree-subject",
            ),
        )
        store.load(
            str(path),
            native_index_authorization=authorization,
        )


def _normalize_symbol(s: str) -> str:
    """Strip trailing parens so `module:Class.method()` and
    `module:Class.method` compare equal."""
    return s[:-2] if s.endswith("()") else s


def _gt_symbol(file_path: str, symbol: str) -> str:
    return f"{file_path}:{_normalize_symbol(symbol)}"


def _make_relative(file_path: str, instance_id: str, repo: str = "") -> str:
    """Best-effort: convert an absolute retrieved path back to a repo-relative
    path that lines up with codenib-base GT.

    Indexed paths come back with whatever absolute prefix was used at index
    time (e.g. ``/home/<user>/.codenib/<owner>_<name>/...`` from
    ``CodeNibBaseDataset.get_repo_path``). GT paths are repo-root-relative
    (e.g. ``astropy/modeling/separable.py``). We look for the repo-name
    segment ``<owner>_<name>`` and return everything after it; if not found,
    fall back to ``<instance_id>``-based stripping; otherwise return as-is.
    """
    if not file_path.startswith("/"):
        return file_path
    parts = Path(file_path).parts
    candidates = []
    if repo:
        candidates.append(repo.replace("/", "_"))
    candidates.append(instance_id)
    for marker in candidates:
        for i, part in enumerate(parts):
            if part == marker:
                return "/".join(parts[i + 1 :])
    return file_path


def compute_metrics(
    retrieved: List[str], ground_truth: List[str], k_values: List[int]
) -> Dict:
    metrics = {}
    gt_set = set(ground_truth)
    for k in k_values:
        top_k = set(retrieved[:k])
        hits = len(top_k & gt_set)
        metrics[str(k)] = {
            "accuracy": 1.0 if hits > 0 else 0.0,
            "precision": hits / k if k > 0 else 0.0,
            "recall": hits / len(gt_set) if gt_set else 0.0,
            "hits": hits,
        }
    return metrics


async def benchmark_instance(
    instance: Dict,
    top_k: int = 20,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    dimension: int = DEFAULT_DIMENSION,
) -> Dict:
    instance_id = instance["instance_id"]
    repo_dir = CODENIB_DATA / instance_id
    language_group = instance.get("language_group", "Python")
    languages = LANGUAGE_GROUP_MAP.get(language_group, ["python"])

    if not repo_dir.exists():
        return {
            "instance_id": instance_id,
            "language_group": language_group,
            "status": "skipped",
            "reason": "index_not_found",
        }

    manifest = RepoManifest(
        repo_path=str(repo_dir),
        commit=instance.get("base_commit", "benchmark"),
        languages=languages,
        file_count=1000,
        indexes={
            "vector": IndexEntry(
                index_type="vector",
                path=str(repo_dir),
                built_at="2024-04-12T20:57:00Z",
                built_at_epoch=1712957820.0,
                status="fresh",
                config={
                    "embedding_model": embedding_model,
                    "embedding_provider": "huggingface",
                    "dimension": dimension,
                    "index_metric": "ip",
                },
            )
        },
    )

    ctx = ServerContext(manifest=manifest)
    try:
        vector_entry = manifest.indexes["vector"]
        vector_contract = dict(vector_entry.config)
        ctx.vector = CodeVectorStore(
            embedding_model=embedding_model,
            embedding_provider="huggingface",
            dimension=dimension,
            index_metric="ip",
            store_path=str(repo_dir),
            revision=(
                DEFAULT_EMBEDDING_REVISION
                if embedding_model == DEFAULT_EMBEDDING_MODEL
                else None
            ),
            trust_remote_code=(embedding_model == DEFAULT_EMBEDDING_MODEL),
            artifact_metadata=vector_contract,
        )
        _load_vector_from_local_admin_boundary(
            ctx.vector,
            vector_entry.path,
            semantic_contract=vector_contract,
            evidence="mcp-benchmark-prebuilt-local-index",
        )
    except Exception as e:
        return {
            "instance_id": instance_id,
            "language_group": language_group,
            "status": "error",
            "reason": str(e),
        }

    stats = ctx.vector.get_stats()
    if stats.get("total_documents", 0) == 0:
        return {
            "instance_id": instance_id,
            "language_group": language_group,
            "status": "skipped",
            "reason": "empty_index",
        }

    query = instance["problem_statement"]
    start_time = time.time()

    try:
        results = await search_semantic(ctx=ctx, query=query, top_k=top_k, level="l2")
        elapsed = time.time() - start_time
    except Exception as e:
        return {
            "instance_id": instance_id,
            "language_group": language_group,
            "status": "error",
            "reason": f"search_failed: {e}",
        }

    repo = instance.get("repo", "") or ""
    retrieved_files: List[str] = []
    retrieved_symbols: List[str] = []
    for r in results:
        rel = _make_relative(r.get("file", "") or "", instance_id, repo)
        retrieved_files.append(rel)
        retrieved_symbols.append(_normalize_symbol(r.get("node_id", "") or ""))

    # GT: prefer gt_target_files / gt_code_blocks (codenib-base schema).
    gt_files = list(instance.get("gt_target_files") or [])
    gt_symbols: List[str] = []
    for block in instance.get("gt_code_blocks") or []:
        if block.get("file_path") and block.get("symbol"):
            gt_symbols.append(_gt_symbol(block["file_path"], block["symbol"]))
    # Fallback to gt_symbols_modified if blocks are absent.
    if not gt_symbols:
        for s in instance.get("gt_symbols_modified") or []:
            gt_symbols.append(_normalize_symbol(s))

    k_values = [1, 3, 5, 10]
    file_metrics = compute_metrics(retrieved_files, gt_files, k_values)
    symbol_metrics = compute_metrics(retrieved_symbols, gt_symbols, k_values)

    return {
        "instance_id": instance_id,
        "language_group": language_group,
        "difficulty_level": instance.get("difficulty_level"),
        "status": "success",
        "elapsed_s": elapsed,
        "num_results": len(results),
        "retrieved_files": retrieved_files[:top_k],
        "retrieved_symbols": retrieved_symbols[:top_k],
        "metrics": {"files": file_metrics, "symbols": symbol_metrics},
    }


def _bucket_aggregate(
    results: List[Dict], key: str, k_values: List[str]
) -> Dict[str, Dict]:
    """Group successful results by `key` and compute mean accuracy@k for files
    and symbols inside each bucket."""
    buckets: Dict[str, List[Dict]] = defaultdict(list)
    for r in results:
        if r["status"] != "success":
            continue
        buckets[str(r.get(key))].append(r)

    out: Dict[str, Dict] = {}
    for bucket_name, items in buckets.items():
        agg = {"files": {}, "symbols": {}, "count": len(items)}
        for k in k_values:
            file_accs = [r["metrics"]["files"][k]["accuracy"] for r in items]
            sym_accs = [r["metrics"]["symbols"][k]["accuracy"] for r in items]
            agg["files"][k] = sum(file_accs) / len(file_accs) if file_accs else 0.0
            agg["symbols"][k] = sum(sym_accs) / len(sym_accs) if sym_accs else 0.0
        out[bucket_name] = agg
    return out


async def run_benchmark(
    output_file: str = "results/mcp_codenib_base.json",
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    dimension: int = DEFAULT_DIMENSION,
    limit: int | None = None,
):
    print(f"Loading {DATASET_NAME} (split={DATASET_SPLIT})...")
    dataset = datasets.load_dataset(DATASET_NAME, split=DATASET_SPLIT)
    if limit is not None:
        dataset = dataset.select(range(min(limit, len(dataset))))
    print(f"Embedding model: {embedding_model} (dim={dimension})")

    Path(output_file).parent.mkdir(parents=True, exist_ok=True)

    results = []
    total = len(dataset)
    success_count = 0
    skipped_count = 0
    error_count = 0

    print(f"Running MCP benchmark on {total} instances...")

    for i, instance in enumerate(dataset, 1):
        print(
            f"\n[{i}/{total}] {instance['instance_id']} "
            f"({instance.get('language_group', '?')})",
            end=" ... ",
        )

        result = await benchmark_instance(
            instance, embedding_model=embedding_model, dimension=dimension
        )
        results.append(result)

        if result["status"] == "success":
            success_count += 1
            print(f"✓ ({result['elapsed_s']:.2f}s)")
        elif result["status"] == "skipped":
            skipped_count += 1
            print(f"⊘ ({result['reason']})")
        else:
            error_count += 1
            print(f"✗ ({result.get('reason', 'unknown')})")

        if i % 10 == 0:
            with open(output_file + ".partial", "w") as f:
                json.dump(
                    {
                        "dataset": DATASET_NAME,
                        "split": DATASET_SPLIT,
                        "method": "mcp_server",
                        "model": embedding_model,
                        "instance_count": i,
                        "success": success_count,
                        "skipped": skipped_count,
                        "errors": error_count,
                        "results": results,
                    },
                    f,
                    indent=2,
                )
            print(f"  [Saved checkpoint: {i} instances]")

    k_values = ["1", "3", "5", "10"]
    aggregate = {"files": {}, "symbols": {}}
    for k in k_values:
        file_accs = [
            r["metrics"]["files"][k]["accuracy"]
            for r in results
            if r["status"] == "success"
        ]
        sym_accs = [
            r["metrics"]["symbols"][k]["accuracy"]
            for r in results
            if r["status"] == "success"
        ]
        aggregate["files"][k] = {
            "accuracy": sum(file_accs) / len(file_accs) if file_accs else 0.0,
            "count": len(file_accs),
        }
        aggregate["symbols"][k] = {
            "accuracy": sum(sym_accs) / len(sym_accs) if sym_accs else 0.0,
            "count": len(sym_accs),
        }

    by_language = _bucket_aggregate(results, "language_group", k_values)
    by_difficulty = _bucket_aggregate(results, "difficulty_level", k_values)

    final_output = {
        "dataset": DATASET_NAME,
        "split": DATASET_SPLIT,
        "method": "mcp_server",
        "embedding_model": embedding_model,
        "instance_count": total,
        "success_count": success_count,
        "skipped_count": skipped_count,
        "error_count": error_count,
        "aggregate_metrics": aggregate,
        "by_language": by_language,
        "by_difficulty": by_difficulty,
        "results": results,
    }

    with open(output_file, "w") as f:
        json.dump(final_output, f, indent=2)

    print(f"\n\n{'='*60}")
    print("MCP Benchmark Complete")
    print(f"{'='*60}")
    print(f"Dataset: {DATASET_NAME} ({DATASET_SPLIT})")
    print(f"Total instances: {total}")
    print(f"Success: {success_count}")
    print(f"Skipped: {skipped_count}")
    print(f"Errors: {error_count}")
    print(f"\nResults saved to: {output_file}")

    print(f"\n{'='*60}")
    print("Aggregate Metrics (File-level)")
    print(f"{'='*60}")
    for k in k_values:
        print(f"Top-{k}: {aggregate['files'][k]['accuracy']:.1%}")

    print(f"\n{'='*60}")
    print("Aggregate Metrics (Symbol-level)")
    print(f"{'='*60}")
    for k in k_values:
        print(f"Top-{k}: {aggregate['symbols'][k]['accuracy']:.1%}")

    if by_language:
        print(f"\n{'='*60}")
        print("By language_group (file-level top-1 / symbol-level top-1, n)")
        print(f"{'='*60}")
        for lang, agg in sorted(by_language.items()):
            print(
                f"  {lang}: {agg['files']['1']:.1%} / "
                f"{agg['symbols']['1']:.1%}  (n={agg['count']})"
            )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default=DEFAULT_EMBEDDING_MODEL)
    p.add_argument("--dimension", type=int, default=DEFAULT_DIMENSION)
    p.add_argument("--output", default="results/mcp_codenib_base.json")
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional cap on number of dataset rows (for smoke runs).",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(
        run_benchmark(
            output_file=args.output,
            embedding_model=args.model,
            dimension=args.dimension,
            limit=args.limit,
        )
    )
