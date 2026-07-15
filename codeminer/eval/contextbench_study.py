# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Frozen external-validity protocol for ContextBench agent-context studies.

The module deliberately separates selection from execution. A protocol is
constructed only from immutable dataset rows and the repository set used to
develop the synthesis experiment. Indexability and measured outcomes never
participate in selection; failures remain in the planned denominator.
"""

from __future__ import annotations

import gc
import hashlib
import json
import re
import subprocess
import time
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence

from codeminer.compiler.snapshot_store import (
    ArtifactProfile,
    SnapshotArtifactStore,
    SourceSnapshot,
    normalize_repo,
)

CONTEXTBENCH_DATASET = "Contextbench/ContextBench"
CONTEXTBENCH_REVISION = "c2855792b006af41c67202d33883fb9d46362853"
CONTEXTBENCH_TEST_FILE = "data/contextbench_verified_test.parquet"
CONTEXTBENCH_TEST_SHA256 = (
    "4b560777bc8ba4061c7afa5f98ca2b5c793d0a32f085f25c5c817db0708b3629"
)
CONTEXTBENCH_SOURCE_COMMIT = "1436c28a8eb95496da4ea69ad458b9f8a8eb7d61"

SYNTHESIS_DATASET = "sysevol-ai/codeminer-synthesis"
SYNTHESIS_REVISION = "5ac36c39ef69bbfe2e14dac58b6067b8c350c53e"
SYNTHESIS_PROTOCOL_SHA256 = (
    "29ebe9ea19722dc681ecb4cac22bbbc748ff9556ad363e9d3b48869ed806c482"
)

# Frozen before ContextBench outcomes are observed. These are the repositories
# used by the 500-query synthesis runtime study, not a hand-selected exclusion.
SYNTHESIS_DEVELOPMENT_REPOSITORIES = (
    "astral-sh/ruff",
    "astropy/astropy",
    "axios/axios",
    "babel/babel",
    "caddyserver/caddy",
    "facebook/docusaurus",
    "fmtlib/fmt",
    "gin-gonic/gin",
    "gohugoio/hugo",
    "hashicorp/terraform",
    "jqlang/jq",
    "matplotlib/matplotlib",
    "micropython/micropython",
    "nushell/nushell",
    "preactjs/preact",
    "prometheus/prometheus",
    "pydata/xarray",
    "redis/redis",
    "scikit-learn/scikit-learn",
    "sharkdp/bat",
    "sympy/sympy",
    "tokio-rs/tokio",
    "uutils/coreutils",
    "valkey-io/valkey",
    "vuejs/core",
)

_LANGUAGE_METADATA = {
    "c": ("C++/C", "cpp", ("cpp",)),
    "go": ("Go", "go", ("go",)),
    "java": ("Java", "java", ("java",)),
    "javascript": ("TypeScript/JavaScript", "typescript", ("js",)),
    "python": ("Python", "python", ("python",)),
    "rust": ("Rust", "rust", ("rust",)),
    "typescript": ("TypeScript/JavaScript", "typescript", ("ts",)),
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    """Hash a file without loading it all into memory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def repository_identity(row: Mapping[str, Any]) -> str:
    """Return an owner/repository identity for every ContextBench source.

    Multi-SWE-bench rows often store only the repository basename (``core`` or
    ``jq``). Treating those values as identities leaks development repositories
    into a nominally disjoint split, so recover the owner from
    ``original_inst_id`` before applying exclusions.
    """

    raw_repo = str(row.get("repo") or "").strip()
    if "/" in raw_repo:
        return normalize_repo(raw_repo)

    original = str(row.get("original_inst_id") or "").strip()
    stem = re.sub(r"-\d+$", "", original)
    if "__" not in stem:
        raise ValueError(
            f"cannot recover repository owner from {original!r} / {raw_repo!r}"
        )
    owner, name = stem.split("__", 1)
    return normalize_repo(f"{owner}/{name}")


def normalize_gold_path(value: Any) -> str:
    """Match ContextBench's official repo-relative path normalization."""

    path = str(value or "").replace("\\", "/")
    if path.startswith("/testbed/"):
        path = path[len("/testbed/") :]
    elif path.startswith("/workspace/"):
        rest = path[len("/workspace/") :]
        path = rest.split("/", 1)[1] if "/" in rest else rest
    else:
        path = path.lstrip("/")
    if path.startswith("./"):
        path = path[2:]
    normalized = str(PurePosixPath(path))
    if not normalized or normalized == "." or ".." in PurePosixPath(normalized).parts:
        raise ValueError(f"invalid gold-context path: {value!r}")
    return normalized


def normalize_gold_context(raw: Any) -> list[dict[str, Any]]:
    """Decode and normalize 1-based inclusive ContextBench spans."""

    if isinstance(raw, str):
        contexts = json.loads(raw)
    else:
        contexts = raw
    if not isinstance(contexts, list):
        raise ValueError("gold_context must decode to a list")

    normalized = []
    seen = set()
    for context in contexts:
        if not isinstance(context, Mapping):
            raise ValueError("gold_context entries must be mappings")
        start = int(context.get("start_line"))
        end = int(context.get("end_line"))
        if start < 1 or end < start:
            raise ValueError(f"invalid gold-context range: {start}-{end}")
        block = {
            "file_path": normalize_gold_path(context.get("file")),
            "start_line": start,
            "end_line": end,
        }
        key = (block["file_path"], start, end)
        if key not in seen:
            seen.add(key)
            normalized.append(block)
    if not normalized:
        raise ValueError("gold_context must contain at least one span")
    return normalized


def normalize_contextbench_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project one source row into the generic query-sweep contract."""

    language = str(row.get("language") or "").lower().strip()
    try:
        language_group, language_key, chunk_languages = _LANGUAGE_METADATA[language]
    except KeyError as exc:
        raise ValueError(f"unsupported ContextBench language: {language!r}") from exc

    instance_id = str(row.get("instance_id") or "").strip()
    original_id = str(row.get("original_inst_id") or "").strip()
    commit = str(row.get("base_commit") or "").lower().strip()
    query = str(row.get("problem_statement") or "").strip()
    if not instance_id or not original_id or not query:
        raise ValueError("ContextBench row is missing an id or problem statement")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError(f"invalid base commit for {instance_id}: {commit!r}")

    raw_gold = row.get("gold_context")
    decoded_gold = json.loads(raw_gold) if isinstance(raw_gold, str) else raw_gold
    blocks = normalize_gold_context(decoded_gold)
    files = list(dict.fromkeys(block["file_path"] for block in blocks))
    source = str(row.get("source") or "").strip()
    normalized = {
        "repo": repository_identity(row),
        "instance_id": instance_id,
        "source_instance_id": original_id,
        "base_commit": commit,
        "query_id": instance_id,
        "query": query,
        "problem_statement": query,
        "category": f"contextbench_{source.lower()}",
        "source_config": f"ContextBench/{source}",
        "language": language,
        "language_group": language_group,
        "language_key": language_key,
        "chunk_languages": list(chunk_languages),
        "gt_files": files,
        "gt_symbols": [],
        "gt_code_blocks": blocks,
        "gold_context_count": len(blocks),
        "source_gold_context_count": len(decoded_gold),
        "exact_duplicate_gold_contexts_removed": len(decoded_gold) - len(blocks),
        "gold_context_sha256": _sha256_text(raw_gold),
        "query_sha256": _sha256_text(query),
        "hints_text_sha256": _sha256_text(row.get("hints_text")),
        "reference_patch_sha256": _sha256_text(row.get("patch")),
        "test_patch_sha256": _sha256_text(row.get("test_patch")),
    }
    normalized["normalized_row_sha256"] = _sha256_json(normalized)
    return normalized


def build_protocol(
    records: Iterable[Mapping[str, Any]],
    *,
    dataset_file_sha256: str,
    development_repositories: Sequence[str] = SYNTHESIS_DEVELOPMENT_REPOSITORIES,
) -> dict[str, Any]:
    """Freeze the repository-disjoint ContextBench external-validity slice."""

    if dataset_file_sha256 != CONTEXTBENCH_TEST_SHA256:
        raise ValueError(
            "ContextBench parquet hash does not match the frozen test artifact: "
            f"{dataset_file_sha256}"
        )
    normalized = [normalize_contextbench_row(row) for row in records]
    if len(normalized) != 50:
        raise ValueError(f"expected 50 frozen test rows, found {len(normalized)}")
    if len({row["instance_id"] for row in normalized}) != len(normalized):
        raise ValueError("ContextBench test rows contain duplicate instance ids")

    development = sorted({normalize_repo(repo) for repo in development_repositories})
    if len(development) != 25:
        raise ValueError(
            f"expected 25 development repositories, found {len(development)}"
        )
    development_set = set(development)
    selected = sorted(
        (row for row in normalized if row["repo"] not in development_set),
        key=lambda row: row["instance_id"],
    )
    excluded = sorted(
        (
            {
                "instance_id": row["instance_id"],
                "source_instance_id": row["source_instance_id"],
                "repo": row["repo"],
                "reason": "repository_overlap_with_synthesis_development",
            }
            for row in normalized
            if row["repo"] in development_set
        ),
        key=lambda row: row["instance_id"],
    )
    selected_repositories = sorted({row["repo"] for row in selected})
    if len(selected) != 41 or len(selected_repositories) != 18:
        raise ValueError(
            "frozen disjoint-slice invariant changed: expected 41 rows / 18 repos, "
            f"found {len(selected)} / {len(selected_repositories)}"
        )

    payload = {
        "schema_version": 1,
        "status": "frozen_before_indexing_or_outcome_measurement",
        "study": "contextbench_repository_disjoint_external_validity_v1",
        "claim_boundary": (
            "validates preload context and localization on issue-driven, human-"
            "annotated tasks; does not measure patch correctness or competing-agent "
            "end-to-end performance"
        ),
        "dataset": {
            "name": CONTEXTBENCH_DATASET,
            "revision": CONTEXTBENCH_REVISION,
            "file": CONTEXTBENCH_TEST_FILE,
            "file_sha256": dataset_file_sha256,
            "source_evaluator_commit": CONTEXTBENCH_SOURCE_COMMIT,
            "source_rows": len(normalized),
        },
        "development_corpus": {
            "name": SYNTHESIS_DATASET,
            "revision": SYNTHESIS_REVISION,
            "runtime_protocol_sha256": SYNTHESIS_PROTOCOL_SHA256,
            "repositories": development,
            "repositories_sha256": _sha256_json(development),
        },
        "selection": {
            "rule": (
                "retain every frozen test row whose normalized owner/repository is "
                "absent from the synthesis development corpus"
            ),
            "outcome_independent": True,
            "index_failures_remain_in_denominator": True,
            "selected_rows": len(selected),
            "selected_repositories": len(selected_repositories),
            "selected_languages": dict(
                sorted(Counter(row["language"] for row in selected).items())
            ),
            "selected_sources": dict(
                sorted(Counter(row["source_config"] for row in selected).items())
            ),
            "repositories": selected_repositories,
            "excluded_rows": excluded,
            "data_quality": {
                "source_gold_spans": sum(
                    row["source_gold_context_count"] for row in selected
                ),
                "normalized_gold_spans": sum(
                    row["gold_context_count"] for row in selected
                ),
                "exact_duplicate_spans_removed": sum(
                    row["exact_duplicate_gold_contexts_removed"] for row in selected
                ),
            },
        },
        "retrieval_protocol": {
            "query": "problem_statement only, matching ContextBench agent prompts",
            "retriever": "Qwen/Qwen3-Embedding-0.6B",
            "retriever_revision": "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3",
            "index": "Flat inner-product over CodeMiner L2 chunks",
            "top_k": 10,
            "primary_metrics": [
                "file_coverage_at_10",
                "line_coverage_at_10",
            ],
            "secondary_metrics": [
                "file_precision_at_10",
                "line_precision_at_10",
                "gold_block_recall_at_10",
            ],
            "metric_alignment": (
                "file and line coverage/precision follow the official ContextBench "
                "set/interval definitions"
            ),
            "path_normalization": (
                "strip /workspace/<checkout>/ and /testbed/ prefixes while "
                "preserving repository dotfiles"
            ),
        },
        "agent_protocol": {
            "model": "Qwen/Qwen3.5-9B",
            "served_model": "openai/qwen3.5-9b",
            "arms": ["grep_only", "preinj_eager", "preinj_eager_compact"],
            "repetitions": 1,
            "planned_cells": len(selected) * 3,
            "primary_metric": "total prompt-plus-completion tokens",
            "quality_guardrail": "paired committed-answer gold-block Recall@5",
            "inference": (
                "paired repository-clustered bootstrap; 18 repository clusters"
            ),
        },
        "rows": selected,
    }
    payload["protocol_payload_sha256"] = _sha256_json(payload)
    return payload


def protocol_rows(protocol: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return normalized query rows after validating the frozen payload hash."""

    payload = dict(protocol)
    expected = payload.pop("protocol_payload_sha256", None)
    if expected != _sha256_json(payload):
        raise ValueError("ContextBench protocol payload hash mismatch")
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("ContextBench protocol contains no rows")
    return [dict(row) for row in rows]


def build_analysis_plan(
    protocol: Mapping[str, Any],
    *,
    protocol_file_sha256: str,
    infrastructure_smoke_sha256: str,
) -> dict[str, Any]:
    """Freeze estimands after one disclosed smoke and before batch evaluation."""

    rows = protocol_rows(protocol)
    return {
        "schema_version": 1,
        "status": "frozen_before_batch_retrieval_and_agent_outcomes",
        "protocol_payload_sha256": protocol["protocol_payload_sha256"],
        "protocol_file_sha256": protocol_file_sha256,
        "disclosed_prior_observation": {
            "scope": "one iamkun/dayjs infrastructure smoke",
            "artifact_sha256": infrastructure_smoke_sha256,
            "reason": (
                "verify checkout, chunking, model revision, retrieval, and metric "
                "plumbing before the batch"
            ),
            "restriction": (
                "the smoke is retained in the final denominator; no arm, cutoff, "
                "metric, or case was selected from its outcome"
            ),
        },
        "population": {
            "issues": len(rows),
            "repositories": len({row["repo"] for row in rows}),
            "languages": sorted({row["language"] for row in rows}),
        },
        "retrieval_estimands": {
            "co_primary": [
                "macro task-level file Coverage@10",
                "macro task-level line Coverage@10",
            ],
            "secondary": [
                "macro task-level gold-block Recall@10",
                "micro context-weighted file Coverage@10 and Precision@10",
                "micro context-weighted line Coverage@10 and Precision@10",
            ],
            "reporting_rule": (
                "report every listed estimand regardless of direction; no success "
                "threshold or post-hoc metric selection"
            ),
            "uncertainty": (
                "10,000-sample owner/repository-clustered percentile bootstrap "
                "with seed 20260713"
            ),
            "gold_residency_sensitivity": (
                "repeat every retrieval estimand after excluding only gold blocks "
                "whose normalized file path is absent from the exact base commit; "
                "retain the official all-gold analysis as primary and report the "
                "excluded file/block ledger"
            ),
        },
        "agent_estimands": {
            "primary": (
                "ratio of summed total tokens to grep_only over successful paired "
                "cells, reported separately for eager and compact"
            ),
            "quality_guardrail": (
                "paired gold-block Recall@5 over explicit committed Locations: "
                "spans only; graph-resolved Symbols: are disabled; report absolute "
                "arm quality and deltas"
            ),
            "quality_margin": -0.05,
            "arms": ["grep_only", "preinj_eager", "preinj_eager_compact"],
            "repetitions": 1,
            "admission_rule": (
                "a policy is confirmatory only if all 123 cells complete and its "
                "repository-clustered 95% lower bound for Recall@5 delta versus "
                "grep_only is at least -0.05"
            ),
            "incomplete_cell_policy": (
                "failed or missing cells contribute zero Recall@5; token ratios "
                "remain descriptive on successful pairs and cannot be admitted "
                "unless the full matrix completes"
            ),
            "cross_workload_rule": (
                "do not pool effects with the synthesis matrix, whose historical "
                "scorer also resolved submitted Symbols: against its graph; use "
                "this issue-driven matrix as an independent gate"
            ),
            "uncertainty": (
                "paired owner/repository-clustered percentile bootstrap; no "
                "language-level significance claims"
            ),
        },
        "failure_policy": (
            "clone/index/query/agent failures stay in the planned ledger; failures "
            "contribute zero to coverage/recall, while precision is conditional on "
            "completed retrievals and completion rate is reported separately"
        ),
        "subgroups": "language and benchmark-source breakdowns are descriptive only",
        "claim_boundary": protocol["claim_boundary"],
    }


def vector_artifact_profile(
    languages: Sequence[str],
    *,
    embedding_model: str,
    embedding_revision: str,
    embedding_dimension: int,
) -> ArtifactProfile:
    """Return the content identity for the frozen vector-only artifact."""

    return ArtifactProfile.create(
        languages,
        name="contextbench-agent-preload-l2-v1",
        options={
            "build_levels": ["l2"],
            "embedding_dimension": embedding_dimension,
            "embedding_model": embedding_model,
            "embedding_revision": embedding_revision,
            "index_metric": "ip",
            "index_type": "flat",
            "max_lines_per_chunk": None,
        },
    )


def ensure_source_repository(
    source_root: str | Path,
    *,
    repo: str,
    commit: str,
) -> Path:
    """Create a partial local clone that contains the requested commit."""

    canonical = normalize_repo(repo)
    root = Path(source_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / canonical.replace("/", "__")
    if not target.exists():
        subprocess.run(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                f"https://github.com/{canonical}.git",
                str(target),
            ],
            check=True,
        )
    if not (target / ".git").exists():
        raise ValueError(f"source cache path is not a git clone: {target}")
    if _git_has_commit(target, commit):
        return target
    subprocess.run(
        ["git", "-C", str(target), "fetch", "--no-tags", "--depth=1", "origin", commit],
        check=True,
    )
    if not _git_has_commit(target, commit):
        raise RuntimeError(f"source clone does not contain {canonical}@{commit}")
    return target


def _git_has_commit(repo: Path, commit: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def prepare_vector_artifacts(
    protocol: Mapping[str, Any],
    *,
    source_root: str | Path,
    output_root: str | Path,
    embedding_model: str,
    embedding_revision: str,
    embedding_dimension: int = 1024,
    batch_size: int = 8,
    instance_ids: Sequence[str] | None = None,
    force: bool = False,
    on_record: Callable[[Mapping[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Materialize vector-only snapshot artifacts, preserving every failure."""

    from codeminer.index.embedding import build_hierarchical_vector_store

    selected = set(instance_ids or ())
    rows = [
        row
        for row in protocol_rows(protocol)
        if not selected or row["instance_id"] in selected
    ]
    if selected - {row["instance_id"] for row in rows}:
        missing = sorted(selected - {row["instance_id"] for row in rows})
        raise ValueError(f"unknown protocol instance ids: {missing}")

    store = SnapshotArtifactStore(output_root)
    shared_embedding = None
    records = []
    for row in rows:
        started = time.perf_counter()
        record = {
            "instance_id": row["instance_id"],
            "repo": row["repo"],
            "base_commit": row["base_commit"],
            "language": row["language"],
        }
        vector_store = None
        try:
            source = ensure_source_repository(
                source_root,
                repo=row["repo"],
                commit=row["base_commit"],
            )
            profile = vector_artifact_profile(
                row["chunk_languages"],
                embedding_model=embedding_model,
                embedding_revision=embedding_revision,
                embedding_dimension=embedding_dimension,
            )
            binding = store.bind(
                row["instance_id"],
                SourceSnapshot(row["repo"], row["base_commit"]),
                profile,
            )
            worktree = store.ensure_worktree(
                binding,
                source_repo=source,
                commit=row["base_commit"],
            )
            suffix = embedding_model.replace("/", "__")
            index_path = binding.profile_dir / "l2" / f"index_{suffix}.faiss"
            docs_path = binding.profile_dir / "l2" / f"documents_{suffix}.pkl"
            cache_hit = index_path.is_file() and docs_path.is_file() and not force
            vector_store = build_hierarchical_vector_store(
                repo_path=str(worktree),
                index_path=str(binding.profile_dir),
                languages=list(row["chunk_languages"]),
                max_lines_per_chunk=None,
                build_levels=["l2"],
                embedding_model=embedding_model,
                embedding_provider="huggingface",
                embedding_dimension=embedding_dimension,
                embedding_kwargs={
                    "revision": embedding_revision,
                    "encode_kwargs": {"batch_size": batch_size},
                },
                embedding=shared_embedding,
                index_metric="ip",
                index_type="flat",
                force_rebuild=force,
            )
            shared_embedding = vector_store.embedding
            actual_commit = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if actual_commit != row["base_commit"]:
                raise ValueError(
                    f"snapshot mismatch: expected {row['base_commit']}, "
                    f"found {actual_commit}"
                )
            missing_gold_files = [
                path for path in row["gt_files"] if not (worktree / path).is_file()
            ]
            document_count = len(vector_store.l2_documents)
            if document_count < 1:
                raise RuntimeError("L2 vector artifact contains no documents")
            record.update(
                {
                    "status": "ready",
                    "cache_hit": cache_hit,
                    "snapshot_id": binding.snapshot_id,
                    "profile_id": binding.profile_id,
                    "profile_dir": str(binding.profile_dir),
                    "l2_documents": document_count,
                    "missing_gold_files": missing_gold_files,
                    "index_sha256": sha256_file(index_path),
                    "documents_sha256": sha256_file(docs_path),
                }
            )
        except Exception as exc:  # noqa: BLE001 - retain failures in ledger.
            record.update(
                {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        finally:
            record["duration_seconds"] = time.perf_counter() - started
            records.append(record)
            if on_record is not None:
                on_record(record)
            if vector_store is not None:
                vector_store.close()
                del vector_store
            gc.collect()
    return records


def _merged_ranges(
    spans: Iterable[Mapping[str, Any]],
) -> dict[str, list[tuple[int, int]]]:
    grouped: dict[str, list[tuple[int, int]]] = {}
    for span in spans:
        path = normalize_gold_path(span.get("file") or span.get("file_path"))
        start = int(span.get("start") or span.get("start_line"))
        end = int(span.get("end") or span.get("end_line"))
        if end < start:
            start, end = end, start
        grouped.setdefault(path, []).append((start, end))

    merged: dict[str, list[tuple[int, int]]] = {}
    for path, ranges in grouped.items():
        ordered = sorted(ranges)
        combined = [ordered[0]]
        for start, end in ordered[1:]:
            previous_start, previous_end = combined[-1]
            if start <= previous_end + 1:
                combined[-1] = (previous_start, max(previous_end, end))
            else:
                combined.append((start, end))
        merged[path] = combined
    return merged


def _range_size(ranges: Mapping[str, Sequence[tuple[int, int]]]) -> int:
    return sum(end - start + 1 for spans in ranges.values() for start, end in spans)


def _range_intersection_size(
    left: Mapping[str, Sequence[tuple[int, int]]],
    right: Mapping[str, Sequence[tuple[int, int]]],
) -> int:
    total = 0
    for path in set(left) & set(right):
        for left_start, left_end in left[path]:
            for right_start, right_end in right[path]:
                total += max(
                    0,
                    min(left_end, right_end) - max(left_start, right_start) + 1,
                )
    return total


def score_context_candidates(
    candidates: Sequence[Mapping[str, Any]],
    gold_blocks: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Score retrieved spans using ContextBench coverage/precision semantics."""

    predicted_ranges = _merged_ranges(candidates)
    gold_ranges = _merged_ranges(gold_blocks)
    predicted_files = set(predicted_ranges)
    gold_files = set(gold_ranges)
    file_intersection = len(predicted_files & gold_files)
    predicted_lines = _range_size(predicted_ranges)
    gold_lines = _range_size(gold_ranges)
    line_intersection = _range_intersection_size(predicted_ranges, gold_ranges)

    block_hits = 0
    for block in gold_blocks:
        block_file = normalize_gold_path(block.get("file") or block.get("file_path"))
        block_start = int(block.get("start") or block.get("start_line"))
        block_end = int(block.get("end") or block.get("end_line"))
        if any(
            candidate_start <= block_end and block_start <= candidate_end
            for candidate_start, candidate_end in predicted_ranges.get(block_file, ())
        ):
            block_hits += 1

    return {
        "file_coverage_at_10": file_intersection / len(gold_files),
        "file_precision_at_10": (
            file_intersection / len(predicted_files) if predicted_files else 1.0
        ),
        "line_coverage_at_10": line_intersection / gold_lines,
        "line_precision_at_10": (
            line_intersection / predicted_lines if predicted_lines else 1.0
        ),
        "gold_block_recall_at_10": block_hits / len(gold_blocks),
        "intersecting_files": file_intersection,
        "predicted_files": len(predicted_files),
        "gold_files": len(gold_files),
        "predicted_lines": predicted_lines,
        "gold_lines": gold_lines,
        "intersecting_lines": line_intersection,
        "gold_block_hits": block_hits,
        "gold_blocks": len(gold_blocks),
    }


def summarize_preload_retrieval(
    protocol: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    bootstrap_samples: int = 10_000,
    seed: int = 20_260_713,
) -> dict[str, Any]:
    """Aggregate retrieval quality with repository-clustered uncertainty."""

    import numpy as np

    if bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be positive")
    rows = protocol_rows(protocol)
    by_instance = {str(record["instance_id"]): record for record in records}
    if len(by_instance) != len(records):
        raise ValueError("retrieval records contain duplicate instance ids")
    unknown = set(by_instance) - {row["instance_id"] for row in rows}
    if unknown:
        raise ValueError(
            f"retrieval records contain unknown instances: {sorted(unknown)}"
        )

    observations = []
    for row in rows:
        record = by_instance.get(row["instance_id"])
        ready = record is not None and record.get("status") == "ready"
        metrics = (
            dict(record["metrics"])
            if ready and isinstance(record.get("metrics"), Mapping)
            else score_context_candidates([], row["gt_code_blocks"])
        )
        observation = {
            "instance_id": row["instance_id"],
            "repo": row["repo"],
            "language": row["language"],
            "source_config": row["source_config"],
            "ready": ready,
            "metrics": metrics,
            "resident_metrics": None,
            "missing_gold_files": None,
            "excluded_gold_blocks": None,
        }
        if record is not None and isinstance(record.get("missing_gold_files"), list):
            missing_files = {
                normalize_gold_path(path) for path in record["missing_gold_files"]
            }
            resident_blocks = [
                block
                for block in row["gt_code_blocks"]
                if normalize_gold_path(block.get("file") or block.get("file_path"))
                not in missing_files
            ]
            observation["missing_gold_files"] = sorted(missing_files)
            observation["excluded_gold_blocks"] = len(row["gt_code_blocks"]) - len(
                resident_blocks
            )
            if resident_blocks:
                observation["resident_metrics"] = score_context_candidates(
                    record.get("candidates", ()) if ready else (),
                    resident_blocks,
                )
        observations.append(observation)

    clusters = sorted({observation["repo"] for observation in observations})
    if len(clusters) != 18:
        raise ValueError(f"expected 18 repository clusters, found {len(clusters)}")
    by_cluster = {
        cluster: [row for row in observations if row["repo"] == cluster]
        for cluster in clusters
    }

    macro_metrics = (
        "file_coverage_at_10",
        "line_coverage_at_10",
        "gold_block_recall_at_10",
    )

    def macro(
        sample: Sequence[Mapping[str, Any]], metric: str, metrics_key: str
    ) -> float:
        return sum(row[metrics_key][metric] for row in sample) / len(sample)

    count_fields = {
        "file_coverage_at_10": ("intersecting_files", "gold_files", False),
        "file_precision_at_10": ("intersecting_files", "predicted_files", True),
        "line_coverage_at_10": ("intersecting_lines", "gold_lines", False),
        "line_precision_at_10": ("intersecting_lines", "predicted_lines", True),
        "gold_block_recall_at_10": ("gold_block_hits", "gold_blocks", False),
    }

    def micro(
        sample: Sequence[Mapping[str, Any]], metric: str, metrics_key: str
    ) -> float:
        numerator, denominator, ready_only = count_fields[metric]
        included = [row for row in sample if row["ready"] or not ready_only]
        numerator_sum = sum(row[metrics_key][numerator] for row in included)
        denominator_sum = sum(row[metrics_key][denominator] for row in included)
        return numerator_sum / denominator_sum if denominator_sum else 1.0

    rng = np.random.default_rng(seed)
    sampled_clusters = rng.choice(
        clusters, size=(bootstrap_samples, len(clusters)), replace=True
    )

    def interval(
        estimator: Any, metric: str, metrics_key: str = "metrics"
    ) -> dict[str, float]:
        point = estimator(observations, metric, metrics_key)
        bootstrap = np.empty(bootstrap_samples, dtype=float)
        for index, sampled in enumerate(sampled_clusters):
            sample = [row for cluster in sampled for row in by_cluster[str(cluster)]]
            bootstrap[index] = estimator(sample, metric, metrics_key)
        low, high = np.percentile(bootstrap, [2.5, 97.5])
        return {
            "estimate": float(point),
            "ci_low": float(low),
            "ci_high": float(high),
        }

    macro_results = {metric: interval(macro, metric) for metric in macro_metrics}
    micro_results = {metric: interval(micro, metric) for metric in count_fields}

    resident_rows = [row for row in observations if row["resident_metrics"] is not None]
    residency_sensitivity: dict[str, Any] = {
        "audited_rows": len(resident_rows),
        "planned_rows": len(observations),
        "available": len(resident_rows) == len(observations),
        "excluded_file_ledger": [
            {
                "instance_id": row["instance_id"],
                "files": row["missing_gold_files"],
                "blocks": row["excluded_gold_blocks"],
            }
            for row in observations
            if row["missing_gold_files"]
        ],
    }
    if residency_sensitivity["available"]:
        residency_sensitivity["estimands"] = {
            "macro_task_level": {
                metric: interval(macro, metric, "resident_metrics")
                for metric in macro_metrics
            },
            "micro_context_weighted": {
                metric: interval(micro, metric, "resident_metrics")
                for metric in count_fields
            },
        }

    subgroup_metrics: dict[str, dict[str, Any]] = {}
    for field in ("language", "source_config"):
        subgroup_metrics[field] = {}
        values = sorted({row[field] for row in observations})
        for value in values:
            subset = [row for row in observations if row[field] == value]
            subgroup_metrics[field][value] = {
                "rows": len(subset),
                "repositories": len({row["repo"] for row in subset}),
                **{
                    metric: macro(subset, metric, "metrics") for metric in macro_metrics
                },
            }

    return {
        "schema_version": 1,
        "statistical_unit": "ContextBench issue/snapshot",
        "cluster_unit": "owner/repository",
        "bootstrap": {
            "method": "repository-clustered percentile bootstrap",
            "samples": bootstrap_samples,
            "seed": seed,
        },
        "completion": {
            "planned_rows": len(observations),
            "ready_rows": sum(row["ready"] for row in observations),
            "failed_or_missing_rows": sum(not row["ready"] for row in observations),
            "repository_clusters": len(clusters),
        },
        "estimands": {
            "macro_task_level": macro_results,
            "micro_context_weighted": micro_results,
            "failure_policy": (
                "failed/missing rows contribute zero to coverage and remain in its "
                "denominator; precision is conditional on ready retrieval rows"
            ),
        },
        "repo_resident_gold_sensitivity": residency_sensitivity,
        "subgroups_descriptive_only": subgroup_metrics,
    }


_CONTEXT_POLICY_ARMS = (
    "grep_only",
    "preinj_eager",
    "preinj_eager_compact",
)


def _agent_recall_at_5(record: Mapping[str, Any] | None) -> float:
    if record is None or not record.get("success"):
        return 0.0
    answer_blocks = (record.get("metrics") or {}).get("answer_blocks") or {}
    metric = answer_blocks.get("5") or answer_blocks.get(5) or {}
    value = metric.get("recall")
    if not isinstance(value, (int, float)):
        raise ValueError(f"successful cell {record.get('cell_id')!r} lacks Recall@5")
    return float(value)


def summarize_agent_context_policies(
    protocol: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    bootstrap_samples: int = 10_000,
    seed: int = 20_260_713,
    quality_margin: float = -0.05,
) -> dict[str, Any]:
    """Summarize the frozen three-arm agent study without outcome filtering."""

    import numpy as np

    if bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be positive")
    rows = protocol_rows(protocol)
    protocol_by_query = {str(row["query_id"]): row for row in rows}
    expected_keys = {
        (query_id, arm)
        for query_id in protocol_by_query
        for arm in _CONTEXT_POLICY_ARMS
    }
    by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    for record in records:
        key = (str(record.get("query_id") or ""), str(record.get("subset_id") or ""))
        if key not in expected_keys:
            raise ValueError(f"agent record is outside the frozen matrix: {key}")
        if key in by_key:
            raise ValueError(f"duplicate agent record: {key}")
        by_key[key] = record

    observations = []
    for query_id, row in protocol_by_query.items():
        observations.append(
            {
                "query_id": query_id,
                "repo": row["repo"],
                "records": {
                    arm: by_key.get((query_id, arm)) for arm in _CONTEXT_POLICY_ARMS
                },
            }
        )
    clusters = sorted({row["repo"] for row in observations})
    if len(clusters) != 18:
        raise ValueError(f"expected 18 repository clusters, found {len(clusters)}")

    def ready(record: Mapping[str, Any] | None) -> bool:
        return bool(
            record is not None
            and record.get("success")
            and isinstance(record.get("total_tokens"), (int, float))
            and float(record["total_tokens"]) > 0
        )

    def clustered_interval(
        sample_rows: Sequence[Mapping[str, Any]],
        value: Callable[[Mapping[str, Any]], float],
        estimator: Callable[[Sequence[float]], float],
        *,
        interval_seed: int,
    ) -> dict[str, float]:
        by_cluster = {
            cluster: [row for row in sample_rows if row["repo"] == cluster]
            for cluster in sorted({row["repo"] for row in sample_rows})
        }
        cluster_names = sorted(by_cluster)
        if not cluster_names:
            raise ValueError("cannot bootstrap an empty agent sample")
        point = estimator([value(row) for row in sample_rows])
        rng = np.random.default_rng(interval_seed)
        bootstrap = np.empty(bootstrap_samples, dtype=float)
        for index in range(bootstrap_samples):
            sampled = rng.choice(cluster_names, size=len(cluster_names), replace=True)
            values = [
                value(row) for cluster in sampled for row in by_cluster[str(cluster)]
            ]
            bootstrap[index] = estimator(values)
        low, high = np.percentile(bootstrap, [2.5, 97.5])
        return {
            "estimate": float(point),
            "ci_low": float(low),
            "ci_high": float(high),
        }

    def mean(values: Sequence[float]) -> float:
        return sum(values) / len(values)

    arm_results: dict[str, Any] = {}
    baseline = "grep_only"
    for arm_index, arm in enumerate(_CONTEXT_POLICY_ARMS):
        completed = [row for row in observations if ready(row["records"][arm])]
        quality = clustered_interval(
            observations,
            lambda row, selected=arm: _agent_recall_at_5(row["records"][selected]),
            mean,
            interval_seed=seed + arm_index * 101,
        )
        arm_result: dict[str, Any] = {
            "completed_rows": len(completed),
            "failed_or_missing_rows": len(observations) - len(completed),
            "mean_tokens_completed_rows": (
                mean([float(row["records"][arm]["total_tokens"]) for row in completed])
                if completed
                else None
            ),
            "absolute_recall_at_5": quality,
        }
        if arm != baseline:
            token_pairs = [
                row
                for row in observations
                if ready(row["records"][baseline]) and ready(row["records"][arm])
            ]

            def ratio(values: Sequence[float]) -> float:
                candidate = sum(value[0] for value in values)
                baseline_total = sum(value[1] for value in values)
                return candidate / baseline_total

            token_ratio = (
                clustered_interval(
                    token_pairs,
                    lambda row, selected=arm: (
                        float(row["records"][selected]["total_tokens"]),
                        float(row["records"][baseline]["total_tokens"]),
                    ),
                    ratio,
                    interval_seed=seed + arm_index * 101 + 1,
                )
                if token_pairs
                else None
            )
            quality_delta = clustered_interval(
                observations,
                lambda row, selected=arm: (
                    _agent_recall_at_5(row["records"][selected])
                    - _agent_recall_at_5(row["records"][baseline])
                ),
                mean,
                interval_seed=seed + arm_index * 101 + 2,
            )
            complete_matrix = len(completed) == len(observations) and all(
                ready(row["records"][baseline]) for row in observations
            )
            arm_result.update(
                {
                    "paired_token_rows": len(token_pairs),
                    "token_ratio_of_sums_vs_grep": token_ratio,
                    "recall_at_5_delta_vs_grep": quality_delta,
                    "quality_margin": quality_margin,
                    "quality_guardrail_pass": quality_delta["ci_low"] >= quality_margin,
                    "confirmatory_admission": bool(
                        complete_matrix and quality_delta["ci_low"] >= quality_margin
                    ),
                }
            )
        arm_results[arm] = arm_result

    missing = [
        {
            "query_id": query_id,
            "arm": arm,
            "status": (
                "missing"
                if by_key.get((query_id, arm)) is None
                else "failed_or_invalid_tokens"
            ),
        }
        for query_id, arm in sorted(expected_keys)
        if not ready(by_key.get((query_id, arm)))
    ]
    return {
        "schema_version": 1,
        "statistical_unit": "ContextBench issue/snapshot",
        "cluster_unit": "owner/repository",
        "bootstrap": {
            "method": "paired repository-clustered percentile bootstrap",
            "samples": bootstrap_samples,
            "seed": seed,
        },
        "completion": {
            "planned_cells": len(expected_keys),
            "recorded_cells": len(by_key),
            "successful_cells": len(expected_keys) - len(missing),
            "failed_or_missing_cells": len(missing),
            "complete_matrix": not missing,
            "repository_clusters": len(clusters),
        },
        "failure_policy": (
            "failed/missing cells contribute zero Recall@5; token ratios use only "
            "successful paired cells and are confirmatory only for a complete matrix"
        ),
        "arms": arm_results,
        "failure_ledger": missing,
    }


def evaluate_preload_retrieval(
    protocol: Mapping[str, Any],
    *,
    prebuilt_root: str | Path,
    embedding_model: str,
    embedding_revision: str,
    embedding_dimension: int = 1024,
    top_k: int = 10,
    instance_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Evaluate frozen preload candidates without invoking an agent model."""

    from codeminer.eval.agent_runner.prebuilt import (
        has_required_indexes,
        instance_dir,
        repo_path_for,
    )
    from codeminer.eval.agent_runner.preload import retrieve_candidates
    from codeminer.eval.retrieval_eval import dedup_spans, nodes_to_spans
    from codeminer.index.embedding.vector_store import CodeVectorStore

    selected = set(instance_ids or ())
    rows = [
        row
        for row in protocol_rows(protocol)
        if not selected or row["instance_id"] in selected
    ]
    shared_embedding = None
    records = []
    for row in rows:
        record = {
            "instance_id": row["instance_id"],
            "repo": row["repo"],
            "base_commit": row["base_commit"],
            "language": row["language"],
        }
        vector_store = None
        if not has_required_indexes(
            str(prebuilt_root), row["instance_id"], embedding_model, {"vector"}
        ):
            record.update({"status": "missing_index", "metrics": None})
            records.append(record)
            continue
        repo_path = Path(repo_path_for(str(prebuilt_root), row["instance_id"]))
        record["missing_gold_files"] = [
            path for path in row["gt_files"] if not (repo_path / path).is_file()
        ]
        try:
            vector_store = CodeVectorStore(
                embedding_model=embedding_model,
                embedding_provider="huggingface",
                dimension=embedding_dimension,
                index_metric="ip",
                embedding=shared_embedding,
                revision=embedding_revision,
            )
            shared_embedding = vector_store.embedding
            vector_store.load(instance_dir(str(prebuilt_root), row["instance_id"]))
            started = time.perf_counter()
            nodes = retrieve_candidates(
                {"retrieve": type("Retrieve", (), {"vector_store": vector_store})()},
                "embedding",
                row["query"],
                top_k,
                "l2",
            )
            query_seconds = time.perf_counter() - started
            candidates = dedup_spans(nodes_to_spans(nodes))[:top_k]
            record.update(
                {
                    "status": "ready",
                    "query_seconds": query_seconds,
                    "candidate_count": len(candidates),
                    "candidates": candidates,
                    "metrics": score_context_candidates(
                        candidates, row["gt_code_blocks"]
                    ),
                }
            )
        except Exception as exc:  # noqa: BLE001 - preserve failed rows.
            record.update(
                {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "metrics": None,
                }
            )
        finally:
            records.append(record)
            if vector_store is not None:
                vector_store.close()
                del vector_store
            gc.collect()
    return records


__all__ = [
    "CONTEXTBENCH_DATASET",
    "CONTEXTBENCH_REVISION",
    "CONTEXTBENCH_TEST_FILE",
    "CONTEXTBENCH_TEST_SHA256",
    "SYNTHESIS_DEVELOPMENT_REPOSITORIES",
    "build_protocol",
    "build_analysis_plan",
    "ensure_source_repository",
    "evaluate_preload_retrieval",
    "normalize_contextbench_row",
    "normalize_gold_context",
    "normalize_gold_path",
    "prepare_vector_artifacts",
    "protocol_rows",
    "repository_identity",
    "score_context_candidates",
    "summarize_agent_context_policies",
    "summarize_preload_retrieval",
    "sha256_file",
    "vector_artifact_profile",
]
