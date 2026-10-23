# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Prepare, retrieve, and rescore the four positive ARB releases."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from filelock import FileLock, Timeout

from codenib.dataset.agent_retrieval_bench import (
    ARB_DATASET,
    ARB_DATASET_REVISION,
    ARB_RELEASES,
    ARBCase,
    contained_path,
    corpus_manifest,
    load_cases,
    load_corpus,
    read_jsonl,
    sha256_file,
)

from .agent_retrieval_bench import (
    ARB_UPSTREAM_REVISION,
    BM25CorpusIndex,
    distribution,
    evaluate_official,
    implementation_receipt,
    json_digest,
    retrieval_usage,
    run_environment,
    sample_split,
    summarize,
    upstream_revision,
    validate_upstream,
)
from .policy_compat import write_json_atomic


def prepare(
    data_root: Path,
    *,
    releases: list[str] | None = None,
    sample_ids: list[str] | None = None,
    limit_per_task: int | None = None,
) -> dict[str, Any]:
    data_root = data_root.resolve()
    releases = list(ARB_RELEASES) if releases is None else releases
    cases = load_cases(
        data_root,
        releases,
        sample_ids=sample_ids or (),
        limit_per_task=limit_per_task,
    )
    inputs = {}
    manifests: dict[str, Any] = {}
    for release in releases:
        samples = contained_path(data_root, f"benchmark/{release}/samples.jsonl")
        inputs[release] = {"samples_sha256": sha256_file(samples)}
        try:
            manifest = contained_path(
                data_root, f"corpus/{release}/corpus_manifest.jsonl"
            )
            inputs[release]["manifest_sha256"] = sha256_file(manifest)
            manifests[release] = corpus_manifest(data_root, release)
        except (KeyError, OSError, TypeError, ValueError) as exc:
            manifests[release] = str(exc)
    snapshots: dict[tuple[str, str, str], dict[str, Any]] = {}
    records = []
    for case in cases:
        sample = case.sample
        key = (case.release, sample["repo"], sample["base_commit"])
        if key not in snapshots:
            try:
                manifest = manifests[case.release]
                if isinstance(manifest, str):
                    raise ValueError(manifest)
                path = manifest[(sample["repo"], sample["base_commit"])]
                digest = sha256_file(path)
                corpus = load_corpus(path, sample["repo"], sample["base_commit"])
                if sha256_file(path) != digest:
                    raise ValueError("corpus changed while preparing")
                snapshots[key] = {
                    "path": str(path.relative_to(data_root)),
                    "sha256": digest,
                    "chunks": len(corpus),
                    "files": sum(row["kind"] == "file" for row in corpus),
                }
            except (KeyError, OSError, TypeError, ValueError) as exc:
                snapshots[key] = {"error": f"{type(exc).__name__}: {exc}"}
        records.append(
            {"release": case.release, "sample": sample, "corpus": snapshots[key]}
        )
    return {
        "schema_version": 1,
        "dataset_source": {
            "repo": ARB_DATASET,
            "expected_revision": ARB_DATASET_REVISION,
            "note": "Local hashes bind inputs; use verified release downloads.",
        },
        "data_root": str(data_root),
        "inputs": inputs,
        "selection": {
            "releases": releases,
            "sample_ids": sorted(sample_ids or []),
            "limit_per_task": limit_per_task,
            "order": "release then sample id",
        },
        "cases": records,
    }


def load_prepared(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or not value.get("cases"):
        raise ValueError("invalid or empty ARB preparation")
    ids = []
    for record in value["cases"]:
        ids.append(ARBCase.from_record(record["release"], record["sample"]).sample_id)
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate sample id in preparation")
    return value


def checked_corpus(
    prepared: dict[str, Any], record: dict[str, Any]
) -> tuple[Path, list[dict[str, Any]]]:
    ref = record["corpus"]
    if ref.get("error"):
        raise ValueError(f"preparation failed: {ref['error']}")
    path = contained_path(Path(prepared["data_root"]), ref["path"])
    if sha256_file(path) != ref["sha256"]:
        raise ValueError("corpus SHA-256 changed since preparation")
    sample = record["sample"]
    rows = load_corpus(path, sample["repo"], sample["base_commit"])
    if sha256_file(path) != ref["sha256"]:
        raise ValueError("corpus changed while reading")
    return path, rows


def load_predictions(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".jsonl":
        return list(read_jsonl(path))
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("predictions must be a JSON array or JSONL objects")
    return rows


def prediction_map(
    predictions: list[dict[str, Any]], prepared: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    selected = {row["sample"]["id"] for row in prepared["cases"]}
    result = {}
    for row in predictions:
        if not isinstance(row, dict):
            raise ValueError("prediction records must be objects")
        sample_id = row.get("sample_id")
        if sample_id not in selected or sample_id in result:
            raise ValueError(f"unknown or duplicate prediction id: {sample_id}")
        result[sample_id] = row
    return result


def evaluate(
    prepared: dict[str, Any],
    predictions: list[dict[str, Any]],
    upstream: Path,
    python: str,
    access: str = "released-corpus",
) -> dict[str, Any]:
    """Score with the pinned evaluator.

    Full-source rankings may name files outside the released candidate set;
    those are dropped before cutoffs and counted. Released-corpus rankings
    must name only released files.
    """
    if access not in SOURCE_ACCESS:
        raise ValueError(f"unknown source access: {access}")
    by_id = prediction_map(predictions, prepared)
    jobs = []
    for record in prepared["cases"]:
        sample = record["sample"]
        job = {
            "sample": sample,
            "prediction": by_id.get(sample["id"]),
            "restrict_to_candidates": access == "full-source",
        }
        try:
            path, _rows = checked_corpus(prepared, record)
            job.update(corpus_path=str(path), corpus_sha256=record["corpus"]["sha256"])
        except (KeyError, OSError, TypeError, ValueError) as exc:
            job["error"] = f"{type(exc).__name__}: {exc}"
        jobs.append(job)
    rows = evaluate_official(jobs, upstream, python)
    for row in rows:
        row["split"] = sample_split(row["sample_id"])
    return {
        "schema_version": 1,
        "evaluator_revision": upstream_revision(upstream),
        "scoring_revision": ARB_UPSTREAM_REVISION,
        "protocol": {
            "mode": "static-positive",
            "source_access": access,
            "candidate_filter": "all_files",
            "mrr_ranking": "full supplied file ranking; see per-case ranking_depth",
            "bcy_rank_depth": 20,
            "bcy_budgets": [4000, 8000, 16000, 32000],
            "bcy_tokenizer": "regex_code_tokenizer_v1",
            "bcy_text": "released kind=file chunks",
            "denominator": "all selected samples; errors contribute zero",
            "split": "dev when the first byte of sha256(sample_id) is even",
        },
        "summary": summarize(rows),
        "cases": rows,
    }


METHODS = ("bm25", "dense", "hybrid", "zoekt", "graph_expansion")
SOURCE_ACCESS = ("released-corpus", "full-source")
GRAPH_TOOLS = (
    "scip-python",
    "scip-go",
    "scip-typescript",
    "rust-analyzer",
    "scip-java",
)


DEFAULT_EMBEDDING = "Qwen/Qwen3-Embedding-0.6B"


def embedding_options(args: argparse.Namespace) -> dict[str, Any] | None:
    """Dense overrides from the CLI; a non-default model needs its revision."""
    model = getattr(args, "embedding_model", None)
    revision = getattr(args, "embedding_revision", None)
    if model and not revision:
        raise ValueError("--embedding-model needs --embedding-revision")
    options = {
        "model": model,
        "revision": revision,
        "max_seq_length": getattr(args, "max_seq_length", None),
        "batch_size": getattr(args, "embedding_batch_size", None),
    }
    return {key: value for key, value in options.items() if value} or None


def zoekt_bin_dir(explicit: Path | None) -> Path:
    if explicit is not None:
        candidates = [explicit]
    else:
        found = shutil.which("zoekt-index")
        tools = os.environ.get("CODENIB_SCIP_TOOLS_DIR")
        candidates = [Path(found).parent] if found else []
        if tools:
            candidates.append(Path(tools) / "go-tools" / "bin")
    for directory in candidates:
        if all((directory / name).is_file() for name in ("zoekt-index", "zoekt")):
            return directory.resolve()
    raise ValueError("zoekt-index and zoekt not found; run `make zoekt-tool`")


def source_access(method: str) -> str:
    return "full-source" if method == "graph_expansion" else "released-corpus"


def _require(module: str, extra: str, method: str) -> None:
    try:
        importlib.import_module(module)
    except ImportError as exc:
        raise ValueError(
            f"{method} needs `pip install 'codenib[{extra}]'`: {exc}"
        ) from exc


def method_config(
    method: str,
    *,
    zoekt_bin: Path | None = None,
    device: str | None = None,
    embedding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Static identity of a native method; part of the resume receipt.

    It also preflights optional dependencies, so a missing extra fails before
    any case is attempted.
    """
    if method not in METHODS:
        raise ValueError(f"unknown ARB method: {method}")
    from . import _arb_methods as methods

    temporary = "temporary directory, removed after each snapshot"

    if method in ("dense", "hybrid"):
        for module in ("sentence_transformers", "faiss"):
            _require(module, "semantic", method)
        resolved, dtype = methods.dense_precision(device)
        options = {
            "model": methods.DENSE_MODEL,
            "revision": methods.DENSE_REVISION,
            "max_seq_length": methods.DENSE_MAX_SEQ_LENGTH,
            "batch_size": methods.DENSE_BATCH_SIZE,
            **{key: value for key, value in (embedding or {}).items() if value},
        }
        if not re.fullmatch(r"[0-9a-f]{40}", str(options["revision"])):
            raise ValueError("--embedding-revision must be a 40-character commit SHA")
        config: dict[str, Any] = {
            **options,
            "device": resolved,
            "dtype": dtype,
            "query_prompt": "",
            "index_location": "memory",
        }
        if method == "hybrid":
            config["fusion"] = f"file-level weighted RRF, k={methods.HYBRID_RRF_K}"
        return config
    if method == "zoekt":
        directory = zoekt_bin_dir(zoekt_bin)
        # The binaries identify the build; their -version flag needs a path.
        return {
            "binaries_sha256": {
                name: sha256_file(directory / name) for name in ("zoekt-index", "zoekt")
            },
            "max_terms": methods.ZOEKT_MAX_TERMS,
            "index_location": temporary,
        }
    if method == "graph_expansion":
        _require("igraph", "graph", method)
        from codenib.toolchains import resolve_command

        # Resolve as the graph builder does: managed toolchain first, then PATH.
        tools = {name: resolve_command(name) for name in GRAPH_TOOLS}
        return {
            "seed_files": methods.GRAPH_SEED_FILES,
            "neighbor_files": methods.GRAPH_NEIGHBOR_FILES,
            "build_timeout_seconds": methods.GRAPH_BUILD_TIMEOUT,
            "min_language_share": methods.GRAPH_MIN_LANGUAGE_SHARE,
            "toolchain_sha256": {
                name: sha256_file(Path(path)) for name, path in tools.items() if path
            },
            "index_location": f"{temporary}; source checkouts kept in --workspace",
        }
    return {"index_location": "memory"}


def method_indexer(
    method: str,
    config: dict[str, Any],
    *,
    zoekt_bin: Path | None = None,
    workspace: Path | None = None,
) -> Callable[[list[dict[str, Any]]], Any]:
    """Build one snapshot index for a native method.

    Dense methods load their embedding model on the first build of a process;
    that build's index carries ``model_load_seconds``.
    """
    if method == "bm25":
        return lambda corpus: BM25CorpusIndex(corpus)
    from . import _arb_methods as methods

    if method == "zoekt":
        directory = zoekt_bin_dir(zoekt_bin)
        return lambda corpus: methods.ZoektCorpusIndex(corpus, directory)
    if method == "graph_expansion":
        if workspace is None:
            raise ValueError("graph_expansion needs --workspace for source checkouts")
        root = workspace.resolve()
        root.mkdir(parents=True, exist_ok=True)

        def build_graph(corpus: list[dict[str, Any]]) -> Any:
            return methods.GraphExpansionIndex(
                corpus,
                repo=corpus[0]["repo"],
                base_commit=corpus[0]["base_commit"],
                workspace=root,
            )

        return build_graph
    encoders: list[Any] = []

    def build(corpus: list[dict[str, Any]]) -> Any:
        loaded = None
        if not encoders:
            encoders.append(methods.DenseEncoder(config))
            loaded = encoders[0].load_seconds
        index_type = (
            methods.DenseCorpusIndex if method == "dense" else methods.HybridCorpusIndex
        )
        index = index_type(corpus, encoders[0])
        index.model_load_seconds = loaded
        return index

    return build


def embedding_usage(
    predictions: list[dict[str, Any]], config: dict[str, Any]
) -> dict[str, Any]:
    """Model use summed over checkpointed builds, so resumes keep earlier work."""
    built = [row for row in predictions if row.get("index_built")]

    def dense_size(row: dict[str, Any]) -> dict[str, Any]:
        size = row["index_size"]
        return size.get("dense", size)

    loads = [
        row["model_load_seconds"] for row in predictions if "model_load_seconds" in row
    ]
    return {
        "model": config["model"],
        "revision": config["revision"],
        "model_loads": len(loads),
        "model_load_seconds": distribution(loads),
        "encoded_documents": sum(dense_size(row)["encoded_documents"] for row in built),
        "reused_documents": sum(dense_size(row)["reused_documents"] for row in built),
    }


def by_corpus(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group cases by corpus snapshot, in first-appearance order."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for position, record in enumerate(cases):
        key = record["corpus"].get("sha256") or f"unprepared:{position}"
        groups.setdefault(key, []).append(record)
    return [record for group in groups.values() for record in group]


def graph_usage(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    built = [row for row in predictions if row.get("index_built")]
    return {
        "samples_with_graph": sum(row.get("graph_used") is True for row in predictions),
        "snapshots_with_graph": sum(
            bool(row["index_size"]["graph"].get("vertices")) for row in built
        ),
        "removed_non_candidate_paths": sum(
            row.get("removed_non_candidate_paths", 0) for row in predictions
        ),
        "source_fetch_seconds": distribution(
            [row["source_fetch_seconds"] for row in built]
        ),
    }


def close_index(index: Any) -> None:
    close = getattr(index, "close", None)
    if close is not None:
        close()


def execute(
    prepared_path: Path,
    output: Path,
    upstream: Path,
    *,
    pred_path: Path | None = None,
    resume: bool = False,
    evaluator_python: str = sys.executable,
    method: str = "bm25",
    zoekt_bin: Path | None = None,
    device: str | None = None,
    workspace: Path | None = None,
    access: str | None = None,
    embedding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One writer per output directory; completed cases survive interruption.

    Lock acquisition is the ownership linearization point. There are no nested
    locks. The owner atomically replaces checkpoints; the OS releases the lock
    on exit/crash. A subsequent explicit resume owns recovery and requires an
    identical input/implementation receipt. Each atomic checkpoint replacement
    commits its predictions and integrity receipt together. Public predictions
    are derived output, never recovery input. Recorded failures are retained.
    """
    prepared = load_prepared(prepared_path)
    upstream = validate_upstream(upstream)
    if pred_path is None:
        if access not in (None, source_access(method)):
            raise ValueError(f"{method} reads {source_access(method)} source")
        access = source_access(method)
    access = access or "released-corpus"
    external = load_predictions(pred_path) if pred_path else None
    if external is not None:
        prediction_map(external, prepared)
    config = {
        "prepared_sha256": json_digest(prepared),
        "method": "external-file-ranking" if pred_path else f"codenib-{method}",
        "method_config": (
            None
            if pred_path
            else method_config(
                method, zoekt_bin=zoekt_bin, device=device, embedding=embedding
            )
        ),
        "candidate_filter": "all_files",
        "source_access": access,
        "upstream_revision": upstream_revision(upstream),
        "scoring_revision": ARB_UPSTREAM_REVISION,
        "implementation": implementation_receipt(),
        "evaluator_python": evaluator_python,
        "external_predictions_sha256": json_digest(external) if pred_path else None,
    }
    output.mkdir(parents=True, exist_ok=True)
    with FileLock(str(output / ".run.lock"), timeout=0):
        config_path = output / "config.json"
        predictions_path = output / "predictions.json"
        checkpoint_path = output / "checkpoint.json"
        result_path = output / "results.json"
        if config_path.exists():
            previous = json.loads(config_path.read_text(encoding="utf-8"))
            if not resume:
                raise ValueError("output already exists; use --resume or a new output")
            if previous != config:
                raise ValueError("resume inputs or implementation changed")
        else:
            if any(path.name != ".run.lock" for path in output.iterdir()):
                raise ValueError("output is nonempty without a matching run receipt")
            write_json_atomic(config_path, config)
        # An interrupted rescore must never leave an old successful report.
        result_path.unlink(missing_ok=True)
        if external is not None:
            predictions = external
            write_json_atomic(predictions_path, predictions)
        else:
            predictions = []
            if checkpoint_path.exists():
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                if (
                    not isinstance(checkpoint, dict)
                    or checkpoint.get("config_sha256") != json_digest(config)
                    or not isinstance(checkpoint.get("predictions"), list)
                    or checkpoint.get("predictions_sha256")
                    != json_digest(checkpoint["predictions"])
                ):
                    raise ValueError("native checkpoint integrity receipt mismatch")
                predictions = checkpoint["predictions"]
            elif predictions_path.exists():
                raise ValueError("native predictions have no recovery checkpoint")
            completed = prediction_map(predictions, prepared)
            build_index = method_indexer(
                method,
                config["method_config"],
                zoekt_bin=zoekt_bin,
                workspace=workspace,
            )
            index_key, index = None, None
            for record in by_corpus(prepared["cases"]):
                case = ARBCase.from_record(record["release"], record["sample"])
                if case.sample_id in completed:
                    continue
                prediction = {
                    "sample_id": case.sample_id,
                    "repo": case.sample["repo"],
                    "base_commit": case.sample["base_commit"],
                    "status": "error",
                    "ranked_files": [],
                    "index_built": False,
                    "tool_calls": 0,
                }
                started = time.perf_counter()
                try:
                    key = record["corpus"].get("sha256")
                    if index is None or key != index_key:
                        # Release the previous snapshot before the next cold build.
                        close_index(index)
                        index_key, index = None, None
                        _path, corpus = checked_corpus(prepared, record)
                        loaded = time.perf_counter()
                        index = build_index(corpus)
                        index_key = key
                        prediction.update(
                            index_built=True,
                            corpus_load_seconds=loaded - started,
                            index_build_seconds=index.build_seconds,
                            index_size=index.size,
                        )
                        if hasattr(index, "fetch_seconds"):
                            prediction["source_fetch_seconds"] = index.fetch_seconds
                        if getattr(index, "model_load_seconds", None) is not None:
                            prediction["model_load_seconds"] = index.model_load_seconds
                    # A failed search is still an attempted call with latency.
                    prediction["tool_calls"] = 1
                    queried = time.perf_counter()
                    try:
                        prediction["ranked_files"] = index.rank(case.query_text)
                    finally:
                        prediction["query_seconds"] = time.perf_counter() - queried
                        prediction["tool_calls"] = index.tool_calls
                        if hasattr(index, "graph_used"):
                            prediction["graph_used"] = index.graph_used
                            prediction["removed_non_candidate_paths"] = (
                                index.removed_non_candidate_paths
                            )
                    prediction["status"] = "ok"
                except Exception as exc:
                    prediction["error"] = f"{type(exc).__name__}: {exc}"
                prediction["retrieval_seconds"] = time.perf_counter() - started
                predictions.append(prediction)
                write_json_atomic(
                    checkpoint_path,
                    {
                        "config_sha256": json_digest(config),
                        "predictions_sha256": json_digest(predictions),
                        "predictions": predictions,
                    },
                )
                write_json_atomic(predictions_path, predictions)
                print(f"{case.sample_id}: {prediction['status']}", file=sys.stderr)
            close_index(index)
            # Recreate a missing, stale or edited public export from the checkpoint.
            write_json_atomic(predictions_path, predictions)
        result = evaluate(prepared, predictions, upstream, evaluator_python, access)
        result["provenance"] = config
        if external is None:
            result["retrieval_usage"] = retrieval_usage(predictions)
            if method in ("dense", "hybrid"):
                result["retrieval_usage"]["embedding"] = embedding_usage(
                    predictions, config["method_config"]
                )
            if method == "graph_expansion":
                result["retrieval_usage"]["graph"] = graph_usage(predictions)
            result["environment"] = run_environment()
        write_json_atomic(result_path, result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codenib-arb", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="freeze cases and validate corpus files")
    prep.add_argument("--data-root", type=Path, required=True)
    prep.add_argument("--release", choices=ARB_RELEASES, action="append")
    prep.add_argument("--sample-id", action="append")
    prep.add_argument("--limit-per-task", type=int)
    prep.add_argument("--output", type=Path, required=True)
    for name in ("run", "evaluate"):
        command = commands.add_parser(name)
        command.add_argument("--prepared", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--upstream", type=Path, required=True)
        command.add_argument("--evaluator-python", default=sys.executable)
        command.add_argument("--resume", action="store_true")
        if name == "run":
            command.add_argument("--method", choices=METHODS, default="bm25")
            command.add_argument(
                "--zoekt-bin-dir",
                type=Path,
                help="directory holding zoekt-index and zoekt (default: PATH, "
                "then $CODENIB_SCIP_TOOLS_DIR/go-tools/bin)",
            )
            command.add_argument("--device", help="dense model device (default: auto)")
            command.add_argument(
                "--embedding-model",
                help=f"dense model on Hugging Face (default: {DEFAULT_EMBEDDING})",
            )
            command.add_argument(
                "--embedding-revision",
                help="40-character model commit; required with --embedding-model",
            )
            command.add_argument(
                "--max-seq-length",
                type=int,
                help="dense token limit for queries and chunks (default: 512)",
            )
            command.add_argument(
                "--embedding-batch-size",
                type=int,
                help="dense batch size (default: 64)",
            )
            command.add_argument(
                "--workspace",
                type=Path,
                help="graph_expansion: directory for base-commit source checkouts",
            )
        else:
            command.add_argument(
                "--source-access",
                choices=SOURCE_ACCESS,
                default="released-corpus",
                help="full-source drops ranked files outside the released set",
            )
        if name == "evaluate":
            command.add_argument("--pred", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(str(args.output) + ".lock", timeout=0):
                if args.output.exists():
                    raise ValueError("preparation already exists; choose a new output")
                prepared = prepare(
                    args.data_root,
                    releases=args.release,
                    sample_ids=args.sample_id,
                    limit_per_task=args.limit_per_task,
                )
                write_json_atomic(args.output, prepared)
            failed = sum(bool(row["corpus"].get("error")) for row in prepared["cases"])
            print(json.dumps({"selected": len(prepared["cases"]), "failed": failed}))
            return 1 if failed else 0
        result = execute(
            args.prepared,
            args.output,
            args.upstream,
            pred_path=getattr(args, "pred", None),
            resume=args.resume,
            evaluator_python=args.evaluator_python,
            method=getattr(args, "method", "bm25"),
            zoekt_bin=getattr(args, "zoekt_bin_dir", None),
            device=getattr(args, "device", None),
            workspace=getattr(args, "workspace", None),
            access=getattr(args, "source_access", None),
            embedding=embedding_options(args),
        )
        print(json.dumps(result["summary"], indent=2))
        return 1 if result["summary"]["overall"]["failed"] else 0
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
        Timeout,
    ) as exc:
        parser.exit(2, f"{type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
