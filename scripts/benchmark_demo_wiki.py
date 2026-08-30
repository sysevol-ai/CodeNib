#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Benchmark a Wiki prose model against cached, source-bound outlines."""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import statistics
import sys
import tempfile
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from codenib.llm.litellm_chat import LiteLLMChat  # noqa: E402
from codenib.log_utils import set_console_log_level  # noqa: E402
from codenib.web.config import load_config  # noqa: E402
from codenib.web.native_authority import authorize_local_manifest_vector  # noqa: E402
from codenib.web.repo_registry import RepoRegistry  # noqa: E402
from codenib.wiki.agent_wiki import AgentWiki  # noqa: E402
from codenib.wiki.sqlite_store import SQLiteWikiStore  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--model")
    parser.add_argument("--api-base")
    parser.add_argument(
        "--api-key-env",
        help="Read a candidate API key from this environment variable",
    )
    parser.add_argument(
        "--model-options-json",
        help="JSON object merged into the candidate model call options",
    )
    parser.add_argument(
        "--repo",
        action="append",
        dest="repos",
        help="Repository id with an already-cached current outline (repeatable)",
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--compact", action="store_true")
    return parser


def evaluate_page(page: dict[str, Any]) -> dict[str, Any]:
    """Extract model-independent Wiki quality and grounding signals."""

    generation = page.get("generation") or {}
    quality = page.get("quality") or {}
    grounding = page.get("grounding") or {}
    return {
        "generation_mode": generation.get("mode"),
        "fallback": generation.get("fallback"),
        "quality_valid": quality.get("valid"),
        "grounding_valid": grounding.get("valid"),
        "citation_coverage": grounding.get("citation_coverage"),
        "evidence_count": grounding.get("evidence_count"),
        "relation_count": grounding.get("relation_count"),
        "citation_count": len(page.get("citations") or []),
        "markdown_chars": len(str(page.get("markdown") or "")),
        "metrics": generation.get("metrics") or {},
    }


def _percentile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return round(ordered[index], 1)


def _summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [run for run in runs if run["status"] == "ok"]
    wall = [float(run["wall_ms"]) for run in successful]
    return {
        "successful": len(successful),
        "failed": len(runs) - len(successful),
        "quality_valid": sum(run.get("quality_valid") is True for run in successful),
        "degraded": sum(run.get("generation_mode") == "degraded" for run in successful),
        "wall_ms": {
            "p50": round(statistics.median(wall), 1) if wall else None,
            "p95": _percentile(wall, 0.95),
            "max": round(max(wall), 1) if wall else None,
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    set_console_log_level("WARNING")
    config = load_config(args.config)
    model = args.model or config.wiki_generation_model
    api_base = args.api_base or config.wiki_generation_api_base
    if args.api_key_env:
        api_key = os.environ.get(args.api_key_env)
        if not api_key:
            raise ValueError(f"environment variable {args.api_key_env!r} is empty")
    elif args.api_base:
        # Local OpenAI-compatible servers usually require a non-empty client
        # value but do not authenticate it. External candidate routes should
        # provide --api-key-env explicitly instead of borrowing another
        # provider's configured secret.
        api_key = "not-needed"
    else:
        api_key = config.wiki_generation_api_key
    options = (
        dict(config.wiki_generation_options or {})
        if args.model is None and args.api_base is None
        else {}
    )
    if args.model_options_json:
        candidate_options = json.loads(args.model_options_json)
        if not isinstance(candidate_options, dict):
            raise ValueError("--model-options-json must contain a JSON object")
        options.update(candidate_options)

    registry = RepoRegistry(
        config,
        native_index_authorization_resolver=authorize_local_manifest_vector,
        allow_missing_native_index_authorization=True,
    )
    registry.load_all()
    selected = list(dict.fromkeys(args.repos or ["psf__requests"]))
    production_cache = os.path.join(os.path.abspath(config.data_dir), "wiki_cache")
    production_store = SQLiteWikiStore(Path(production_cache) / "wiki.sqlite3")
    outlines: dict[str, dict[str, Any]] = {}
    bundles: dict[str, Any] = {}
    for repo_id in selected:
        bundle = registry.get(repo_id)
        if bundle is None:
            raise ValueError(f"unknown repository id: {repo_id!r}")
        reference = AgentWiki(
            bundle,
            model=config.wiki_generation_model,
            cache_dir=production_cache,
            store=production_store,
        )
        outline = reference._read_cache("outline")
        if not isinstance(outline, dict) or not outline.get("pages"):
            raise RuntimeError(
                f"{repo_id} has no current cached outline; prewarm it before "
                "running a model comparison"
            )
        outlines[repo_id] = outline
        bundles[repo_id] = bundle

    runs: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="codenib-wiki-benchmark-") as cache_root:
        for repetition in range(max(1, args.repeat)):
            for repo_id in selected:
                record: dict[str, Any] = {
                    "repo": repo_id,
                    "page": "overview",
                    "repetition": repetition + 1,
                }
                run_cache = os.path.join(cache_root, repo_id, str(repetition + 1))
                llm = LiteLLMChat(
                    model=model,
                    temperature=0.2,
                    max_tokens=4096,
                    api_base=api_base,
                    api_key=api_key,
                    extra_kwargs=options,
                )
                wiki = AgentWiki(
                    bundles[repo_id],
                    model=model,
                    cache_dir=run_cache,
                    store=SQLiteWikiStore(Path(run_cache) / "wiki.sqlite3"),
                    llm=llm,
                    api_base=api_base,
                    api_key=api_key,
                )
                wiki._outline = copy.deepcopy(outlines[repo_id])
                started = perf_counter()
                try:
                    page = wiki.page("overview")
                    if not isinstance(page, dict):
                        raise RuntimeError("Overview generation returned no page")
                    record.update(evaluate_page(page))
                    record.update(
                        {
                            "status": "ok",
                            "wall_ms": round((perf_counter() - started) * 1000, 1),
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - retain other cells
                    record.update(
                        {
                            "status": "error",
                            "wall_ms": round((perf_counter() - started) * 1000, 1),
                            "error": str(exc),
                        }
                    )
                runs.append(record)

    report = {
        "schema_version": 1,
        "candidate": args.candidate,
        "model": model,
        "api_base": str(api_base) if api_base is not None else None,
        "repositories": selected,
        "repeat": max(1, args.repeat),
        "summary": _summary(runs),
        "runs": runs,
    }
    print(
        json.dumps(
            report,
            indent=None if args.compact else 2,
            separators=(",", ":") if args.compact else None,
            sort_keys=True,
        )
    )
    failed = any(
        run["status"] != "ok"
        or run.get("quality_valid") is not True
        or run.get("generation_mode") == "degraded"
        for run in runs
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
