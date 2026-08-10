#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Run a SweRank retrieve-then-rerank recipe over a local repository.

The default recipe uses ``SweRankEmbed-Small`` to retrieve 100 code chunks,
passes the first 30 to ``SweRankLLM-Small`` using its RankGPT-style listwise
contract, and returns the requested top-k. A one-process dual-encoder variant
uses ``SweRankEmbed-Large`` for the second-stage candidate scoring instead.

Examples:
    # SweRankLLM route. Start the vLLM command documented in examples/README.md.
    python examples/swerank_retrieve_rerank.py . \
        --query "Configuration reload ignores changes to nested includes"

    # One-process alternative; no LLM endpoint is required.
    python examples/swerank_retrieve_rerank.py . \
        --query "Configuration reload ignores changes to nested includes" \
        --reranker embed-large

SweRank model weights are CC-BY-NC-4.0. Review their model cards before using
this recipe outside research or other non-commercial settings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Sequence

RETRIEVAL_MODEL = "Salesforce/SweRankEmbed-Small"
RETRIEVAL_REVISION = "745d2a06103a66d3cfa600aa52fc0d3523010daa"
RETRIEVAL_DIMENSION = 768
EMBED_RERANK_MODEL = "Salesforce/SweRankEmbed-Large"
EMBED_RERANK_REVISION = "b2cbc7dd3a2aed8c268fa65673464cd5ad96f9ff"
EMBED_RERANK_DIMENSION = 3584
LLM_RERANK_MODEL = "openai/swerank-llm-small"
RETRIEVAL_TOP_K = 100
DEFAULT_CANDIDATE_TOP_K = 30
CACHE_PROFILE_VERSION = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search one repository with a SweRank retrieve-rerank cascade.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "repo",
        nargs="?",
        default=".",
        help="Repository checkout to index and search.",
    )
    query = parser.add_mutually_exclusive_group(required=True)
    query.add_argument("--query", help="Issue or change request to localize.")
    query.add_argument(
        "--query-file",
        type=Path,
        help="UTF-8 file containing the issue or change request.",
    )
    parser.add_argument(
        "--reranker",
        choices=("llm-small", "embed-large"),
        default="llm-small",
        help=(
            "Second-stage SweRank model. llm-small uses the listwise model "
            "recipe; embed-large is a one-process dual-encoder alternative."
        ),
    )
    parser.add_argument(
        "--llm-model",
        default=LLM_RERANK_MODEL,
        help=(
            "LiteLLM model identifier for the OpenAI-compatible "
            "SweRankLLM-Small server. Used only with --reranker llm-small."
        ),
    )
    parser.add_argument(
        "--api-base",
        default="http://127.0.0.1:9000/v1",
        help=(
            "OpenAI-compatible endpoint for SweRankLLM-Small. Used only with "
            "--reranker llm-small."
        ),
    )
    parser.add_argument(
        "--language",
        action="append",
        default=[],
        help="Chunker language; repeat for multiple languages (auto-detected by default).",
    )
    parser.add_argument(
        "--index-dir",
        type=Path,
        default=None,
        help=(
            "Vector-index cache. The default is isolated by repository "
            "content and index build profile."
        ),
    )
    parser.add_argument(
        "--candidate-top-k",
        type=int,
        default=DEFAULT_CANDIDATE_TOP_K,
        help="First-stage candidates passed to the reranker.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=10,
        help="Final number of reranked code chunks to return.",
    )
    parser.add_argument(
        "--retrieval-batch-size",
        type=int,
        default=32,
        help="SweRankEmbed-Small encoding batch size.",
    )
    parser.add_argument(
        "--rerank-batch-size",
        type=int,
        default=2,
        help="SweRankEmbed-Large encoding batch size.",
    )
    parser.add_argument(
        "--max-lines-per-chunk",
        type=int,
        default=300,
        help="Maximum source lines in one indexed chunk.",
    )
    parser.add_argument(
        "--device",
        default=None,
        help="Sentence-Transformers device for embedding models (for example cuda:0).",
    )
    parser.add_argument(
        "--snippet-lines",
        type=int,
        default=8,
        help="Source lines printed for each result; use 0 to hide snippets.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable results, including complete chunk content.",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.top_k <= 0:
        parser.error("--top-k must be positive")
    if not args.top_k <= args.candidate_top_k <= RETRIEVAL_TOP_K:
        parser.error(
            "--candidate-top-k must be between --top-k and the "
            f"{RETRIEVAL_TOP_K}-candidate retrieval width"
        )
    for name in (
        "retrieval_batch_size",
        "rerank_batch_size",
        "max_lines_per_chunk",
    ):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.snippet_lines < 0:
        parser.error("--snippet-lines cannot be negative")
    return args


def read_query(args: argparse.Namespace) -> str:
    text = args.query
    if args.query_file is not None:
        text = args.query_file.expanduser().read_text(encoding="utf-8")
    normalized = (text or "").strip()
    if not normalized:
        raise ValueError("query must not be empty")
    return normalized


def resolve_languages(repo: Path, requested: Sequence[str]) -> list[str]:
    from codenib.cli import detect_languages, normalize_languages

    languages = normalize_languages(requested) if requested else detect_languages(repo)
    if not languages:
        raise ValueError(
            "CodeNib found no supported source language; pass --language explicitly"
        )
    return languages


def _cache_profile_key(languages: Sequence[str], max_lines_per_chunk: int) -> str:
    profile = {
        "version": CACHE_PROFILE_VERSION,
        "embedding_model": RETRIEVAL_MODEL,
        "embedding_dimension": RETRIEVAL_DIMENSION,
        "languages": list(languages),
        "max_lines_per_chunk": max_lines_per_chunk,
        "retrieval_level": "l2",
        "normalize_embeddings": True,
        "index_metric": "ip",
    }
    encoded = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(encoded).hexdigest()[:16]
    return f"profile-v{CACHE_PROFILE_VERSION}-{digest}"


def resolve_index_dir(
    args: argparse.Namespace, repo: Path, languages: Sequence[str]
) -> Path:
    if args.index_dir is not None:
        return args.index_dir.expanduser().resolve()

    from codenib.compiler.checkout_identity import checkout_commit
    from codenib.paths import repo_state_dir
    from codenib.source_fingerprint import (
        fingerprint_repository,
        repository_source_is_dirty,
    )

    commit = checkout_commit(repo)
    if commit and not repository_source_is_dirty(repo):
        source_key = f"git-{commit.lower()}"
    else:
        fingerprint = fingerprint_repository(repo).value.replace(":", "-", 1)
        source_key = f"source-{fingerprint}"

    profile_key = _cache_profile_key(languages, args.max_lines_per_chunk)
    return (
        repo_state_dir(repo)
        / "recipes"
        / "swerank-embed-small"
        / source_key
        / profile_key
    )


def _embedding_runtime_kwargs(
    batch_size: int,
    device: str | None,
    revision: str,
) -> dict[str, Any]:
    model_kwargs = {"device": device} if device else {}
    return {
        "trust_remote_code": True,
        "revision": revision,
        "model_kwargs": model_kwargs,
        "encode_kwargs": {
            "batch_size": batch_size,
            "normalize_embeddings": True,
        },
    }


def build_pipeline_kwargs(
    args: argparse.Namespace,
    *,
    repo: Path,
    index_dir: Path,
    languages: Sequence[str],
) -> dict[str, Any]:
    """Return the explicit pipeline contract without loading either model."""
    from codenib.model import RetrieveStageConfig

    kwargs: dict[str, Any] = {
        "repo_path": str(repo),
        "index_path": str(index_dir),
        "retrieval_plan": [RetrieveStageConfig(engine="dense", top_k=RETRIEVAL_TOP_K)],
        "retrieval_mode": "dense",
        "retrieval_level": "l2",
        "embedding_model": RETRIEVAL_MODEL,
        "embedding_provider": "huggingface",
        "embedding_dimension": RETRIEVAL_DIMENSION,
        "embedding_model_kwargs": _embedding_runtime_kwargs(
            args.retrieval_batch_size,
            args.device,
            RETRIEVAL_REVISION,
        ),
        "languages": list(languages),
        "max_lines_per_chunk": args.max_lines_per_chunk,
        "rerank_candidate_top_k": args.candidate_top_k,
    }

    if args.reranker == "embed-large":
        kwargs.update(
            rerank_strategy="embedding",
            rerank_embedding_model=EMBED_RERANK_MODEL,
            rerank_embedding_provider="huggingface",
            rerank_embedding_dimension=EMBED_RERANK_DIMENSION,
            rerank_embedding_model_kwargs=_embedding_runtime_kwargs(
                args.rerank_batch_size,
                args.device,
                EMBED_RERANK_REVISION,
            ),
        )
    else:
        kwargs.update(
            rerank_strategy="llm",
            rerank_model=args.llm_model,
            rerank_listwise_format="rankgpt",
            rerank_window_size=10,
            rerank_window_step=10,
        )
    return kwargs


def _repository_relative_file(value: str | None, repo: Path | None) -> str | None:
    if value is None or repo is None:
        return value
    path = Path(value)
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(repo).as_posix()
    except ValueError:
        return str(path)


def _result_payload(result: Any, rank: int, repo: Path | None) -> dict[str, Any]:
    start_line = result.start_line + 1 if result.start_line is not None else None
    end_line = result.end_line + 1 if result.end_line is not None else None
    return {
        "rank": rank,
        "file": _repository_relative_file(result.file, repo),
        "start_line": start_line,
        "end_line": end_line,
        "symbol": result.node_name,
        "type": result.type,
        "score": result.score,
        "content": result.content,
    }


def render_results(
    results: Sequence[Any],
    *,
    snippet_lines: int,
    as_json: bool,
    repo: Path | None = None,
) -> None:
    payload = [
        _result_payload(result, rank, repo) for rank, result in enumerate(results, 1)
    ]
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    if not payload:
        print("No code chunks matched the query.")
        return

    for item in payload:
        location = item["file"] or "<unknown>"
        if item["start_line"] is not None:
            location = f"{location}:{item['start_line']}"
        symbol = item["symbol"] or item["type"] or "<chunk>"
        score = item["score"]
        score_text = f" score={score:.4f}" if score is not None else ""
        print(f"[{item['rank']}] {location}  {symbol}{score_text}")
        if snippet_lines and item["content"]:
            for line in str(item["content"]).splitlines()[:snippet_lines]:
                print(f"    {line}")


def run(args: argparse.Namespace) -> int:
    from codenib.source_fingerprint import lexical_repository_path

    repo = lexical_repository_path(args.repo)
    if not repo.is_dir():
        raise ValueError(f"repository directory does not exist: {repo}")

    query = read_query(args)
    languages = resolve_languages(repo, args.language)
    index_dir = resolve_index_dir(args, repo, languages)

    if args.reranker == "llm-small":
        os.environ["OPENAI_API_BASE"] = args.api_base
        if args.api_base.startswith(("http://127.0.0.1", "http://localhost")):
            os.environ.setdefault("OPENAI_API_KEY", "local")

    from codenib.model import RetrieveRerankPipeline

    pipeline = RetrieveRerankPipeline(
        **build_pipeline_kwargs(
            args,
            repo=repo,
            index_dir=index_dir,
            languages=languages,
        )
    )
    try:
        results = pipeline.query(query, top_k=args.top_k)
    finally:
        pipeline.close()

    render_results(
        results,
        snippet_lines=args.snippet_lines,
        as_json=args.json,
        repo=repo,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"error: {exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
